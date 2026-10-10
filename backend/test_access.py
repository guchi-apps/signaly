"""StatusHub 判定APIクライアント（access.py）の契約テスト（#319）。通信はしない。"""

import os
import unittest
import urllib.error
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

import access  # noqa: E402
import supabase_auth  # noqa: E402

SUBJECT = access.Subject(sub="u1", email="You@Example.com", email_verified=True)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def response(allowed=True, version=3, ttl=30, stale=300, with_decision=True):
    decision = access.Decision(allowed=allowed, permissions=("member",) if allowed else (), reason=None if allowed else "revoked")
    return access.Response(version, ttl, stale, decision if with_decision else None)


class ScriptedFetcher:
    """呼び出しごとに、用意した応答（または例外）を順に返す。"""

    def __init__(self, *script):
        self.script = list(script)
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class DecideTest(unittest.TestCase):
    def make(self, *script):
        clock = FakeClock()
        fetcher = ScriptedFetcher(*script)
        return access.AccessClient(fetcher=fetcher, now=clock), fetcher, clock

    def test_allowed_and_cached_within_ttl(self):
        client, fetcher, clock = self.make(response())
        self.assertTrue(client.decide(SUBJECT).allowed)
        clock.t += 29
        self.assertTrue(client.decide(SUBJECT).allowed)
        self.assertEqual(len(fetcher.bodies), 1)

    def test_refetched_after_ttl_and_reports_applied_version(self):
        client, fetcher, clock = self.make(response(version=3), response(allowed=False, version=4))
        self.assertTrue(client.decide(SUBJECT).allowed)
        clock.t += 31
        self.assertFalse(client.decide(SUBJECT).allowed)
        self.assertNotIn("appliedVersion", fetcher.bodies[0])
        self.assertEqual(fetcher.bodies[1]["appliedVersion"], 3)

    def test_failure_uses_previous_decision_until_max_stale(self):
        client, _, clock = self.make(response(), OSError("down"), OSError("down"))
        self.assertTrue(client.decide(SUBJECT).allowed)
        clock.t += 100
        self.assertTrue(client.decide(SUBJECT).allowed)
        clock.t += 201  # 取得できた時刻から 301 秒
        self.assertFalse(client.decide(SUBJECT).allowed)

    def test_failure_without_previous_decision_denies(self):
        client, _, _ = self.make(urllib.error.URLError("down"))
        self.assertFalse(client.decide(SUBJECT).allowed)

    def test_malformed_response_is_a_failure(self):
        client, _, _ = self.make(access.AccessUnavailable("unexpected payload"))
        self.assertFalse(client.decide(SUBJECT).allowed)

    def test_unverified_identity_is_denied_without_a_request(self):
        client, fetcher, _ = self.make()
        self.assertEqual(client.decide(access.Subject("u1", "a@example.com", False)).reason, "unverified_identity")
        self.assertEqual(client.decide(access.Subject("", "a@example.com", True)).reason, "unverified_identity")
        self.assertEqual(fetcher.bodies, [])

    def test_revoked_user_is_denied(self):
        client, _, _ = self.make(response(allowed=False))
        self.assertFalse(client.decide(SUBJECT).allowed)

    def test_heartbeat_sends_no_subject_and_records_version(self):
        client, fetcher, _ = self.make(response(version=7, with_decision=False), response(version=8))
        self.assertTrue(client.heartbeat())
        self.assertNotIn("subject", fetcher.bodies[0])
        client.decide(SUBJECT)
        self.assertEqual(fetcher.bodies[1]["appliedVersion"], 7)

    def test_heartbeat_failure_returns_false(self):
        client, _, _ = self.make(OSError("down"))
        self.assertFalse(client.heartbeat())


class ParseResponseTest(unittest.TestCase):
    def test_rejects_bad_shapes(self):
        for payload in (None, [], {}, {"appVersion": "1", "ttlSeconds": 30, "maxStaleSeconds": 300},
                        {"appVersion": 1, "ttlSeconds": -1, "maxStaleSeconds": 300},
                        {"appVersion": True, "ttlSeconds": 30, "maxStaleSeconds": 300}):
            with self.assertRaises(access.AccessUnavailable):
                access.parse_response(payload, expect_decision=False)

    def test_decision_required_when_expected(self):
        base = {"appVersion": 1, "ttlSeconds": 30, "maxStaleSeconds": 300}
        with self.assertRaises(access.AccessUnavailable):
            access.parse_response(base, expect_decision=True)
        parsed = access.parse_response({**base, "decision": {"allowed": False, "reason": "revoked", "permissions": ["x"]}}, True)
        self.assertFalse(parsed.decision.allowed)
        self.assertEqual(parsed.decision.permissions, ())  # 拒否のとき権限は持ち越さない


class HttpFetcherTest(unittest.TestCase):
    def setUp(self):
        access.forget_shared_token()
        self.addCleanup(access.forget_shared_token)

    def test_missing_token_fails_without_request(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(access, "get_shared_token", lambda: None), \
                patch.object(access, "_post") as post:
            with self.assertRaises(access.AccessUnavailable):
                access.http_fetcher({"subject": SUBJECT.payload()})
        post.assert_not_called()

    def test_401_rereads_token_and_retries_once(self):
        tokens = iter(["old", "new"])
        calls = []
        ok = {"appVersion": 1, "ttlSeconds": 30, "maxStaleSeconds": 300, "decision": {"allowed": True, "permissions": []}}

        def post(token, body):
            calls.append(token)
            if token == "old":
                raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
            return ok

        with patch.object(access, "get_shared_token", lambda: next(tokens)), patch.object(access, "_post", post):
            result = access.http_fetcher({"subject": SUBJECT.payload()})
        self.assertEqual(calls, ["old", "new"])
        self.assertTrue(result.decision.allowed)

    def test_legacy_env_allowlist_is_not_consulted(self):
        """旧 ALLOWED_EMAILS が設定されていても、判定に使われないこと。"""
        with patch.dict(os.environ, {"ALLOWED_EMAILS": "you@example.com"}), \
                patch.object(access, "get_shared_token", lambda: None):
            client = access.AccessClient(fetcher=access.http_fetcher)
            self.assertFalse(client.decide(SUBJECT).allowed)


class EmailVerifiedClaimTest(unittest.TestCase):
    def test_only_app_metadata_google_counts(self):
        f = supabase_auth.email_verified_from_claims
        self.assertTrue(f({"app_metadata": {"provider": "google"}}))
        self.assertTrue(f({"app_metadata": {"providers": ["email", "google"]}}))
        self.assertFalse(f({"app_metadata": {"provider": "email"}}))
        self.assertFalse(f({}))
        # user_metadata は利用者が書き換えられるので根拠にしない
        self.assertFalse(f({"user_metadata": {"email_verified": True}}))


if __name__ == "__main__":
    unittest.main()
