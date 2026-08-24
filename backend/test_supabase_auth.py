"""Supabase Auth の JWT 検証と、認証まわりのエンドポイントのテスト

JWKS は本物の Supabase を叩かず、テスト内で生成した EC 鍵で差し替える。
確認したいのは「署名を本当に検証しているか」なので、鍵の出どころ以外は本物のコードを通す。

環境変数ではなくモジュール属性を差し替えているのは、unittest discover が
1 プロセスで全テストを読み込み、main / auth の import 順に左右されないようにするため。
"""

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
import supabase_auth  # noqa: E402

SUPABASE_URL = "https://test-project.supabase.co"
PUBLISHABLE_KEY = "sb_publishable_test"
ISSUER = f"{SUPABASE_URL}/auth/v1"
ALLOWED_EMAIL = "you@example.com"
USER_ID = "00000000-0000-4000-8000-000000000001"

_private_key = ec.generate_private_key(ec.SECP256R1())
_public_key = _private_key.public_key()
_other_private_key = ec.generate_private_key(ec.SECP256R1())


def make_token(key=None, algorithm="ES256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": USER_ID,
        "email": ALLOWED_EMAIL,
        "aud": supabase_auth.AUDIENCE,
        "iss": ISSUER,
        "iat": now,
        "exp": now + 3600,
        "role": "authenticated",
    }
    for key_name, value in overrides.items():
        if value is None:
            claims.pop(key_name, None)
        else:
            claims[key_name] = value
    return jwt.encode(claims, key or _private_key, algorithm=algorithm)


class SupabaseAuthTestBase(unittest.TestCase):
    """SUPABASE_* とテスト用の署名鍵を差し込む。"""

    def setUp(self):
        supabase_auth.reset_jwks_cache()
        self.addCleanup(supabase_auth.reset_jwks_cache)

        patches = [
            patch.object(supabase_auth, "SUPABASE_URL", SUPABASE_URL),
            patch.object(supabase_auth, "SUPABASE_PUBLISHABLE_KEY", PUBLISHABLE_KEY),
            # JWKS の取得だけを差し替える。検証そのものは本物の jwt.decode を通す
            patch.object(
                supabase_auth,
                "_client",
                lambda: SimpleNamespace(
                    get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_public_key)
                ),
            ),
            patch.object(auth, "ALLOWED_EMAILS", {ALLOWED_EMAIL}),
            patch.object(auth, "SECRET_KEY", "test-secret-key"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class VerifyAccessTokenTest(SupabaseAuthTestBase):
    def test_valid_token_returns_claims(self):
        claims = supabase_auth.verify_access_token(make_token())
        self.assertEqual(claims["email"], ALLOWED_EMAIL)
        self.assertEqual(supabase_auth.user_id_from_claims(claims), USER_ID)

    def test_expired_token_is_rejected(self):
        now = int(time.time())
        token = make_token(exp=now - 10, iat=now - 3600)
        with self.assertRaises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(token)

    def test_other_issuer_is_rejected(self):
        token = make_token(iss="https://evil.supabase.co/auth/v1")
        with self.assertRaises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(token)

    def test_other_audience_is_rejected(self):
        with self.assertRaises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(make_token(aud="anon"))

    def test_token_signed_by_another_key_is_rejected(self):
        """署名検証をしていなければ、ここが通ってしまう。"""
        token = make_token(key=_other_private_key)
        with self.assertRaises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(token)

    def test_tampered_payload_is_rejected(self):
        header, payload, signature = make_token().split(".")
        forged_payload = jwt.utils.base64url_encode(
            b'{"sub":"x","email":"attacker@example.com","aud":"authenticated",'
            + f'"iss":"{ISSUER}","exp":{int(time.time()) + 3600}'.encode()
            + b"}"
        ).decode()
        with self.assertRaises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(f"{header}.{forged_payload}.{signature}")

    def test_hs256_token_is_rejected(self):
        """alg 混同（対称鍵で署名したトークン）を受け付けない。"""
        token = jwt.encode(
            {
                "sub": USER_ID,
                "email": ALLOWED_EMAIL,
                "aud": supabase_auth.AUDIENCE,
                "iss": ISSUER,
                "exp": int(time.time()) + 3600,
            },
            "x" * 32,
            algorithm="HS256",
        )
        with self.assertRaises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(token)

    def test_missing_required_claim_is_rejected(self):
        with self.assertRaises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(make_token(sub=None))

    def test_not_configured_raises_503(self):
        with patch.object(supabase_auth, "SUPABASE_URL", ""):
            with self.assertRaises(supabase_auth.SupabaseAuthError) as ctx:
                supabase_auth.verify_access_token(make_token())
        self.assertEqual(ctx.exception.status, 503)

    def test_email_is_normalized(self):
        claims = supabase_auth.verify_access_token(make_token(email="You@Example.com "))
        self.assertEqual(supabase_auth.email_from_claims(claims), ALLOWED_EMAIL)


class AuthEndpointTest(SupabaseAuthTestBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app)

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_config_returns_public_values(self):
        res = self.client.get("/api/auth/config")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(),
            {"supabaseUrl": SUPABASE_URL, "supabasePublishableKey": PUBLISHABLE_KEY},
        )

    def test_config_is_503_when_not_configured(self):
        with patch.object(supabase_auth, "SUPABASE_URL", ""):
            res = self.client.get("/api/auth/config")
        self.assertEqual(res.status_code, 503)

    def test_me_requires_auth(self):
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_me_accepts_valid_token(self):
        res = self.client.get("/auth/me", headers=self.bearer(make_token()))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"email": ALLOWED_EMAIL})

    def test_me_rejects_invalid_token(self):
        res = self.client.get("/auth/me", headers=self.bearer("not-a-jwt"))
        self.assertEqual(res.status_code, 401)

    def test_disallowed_email_gets_403(self):
        """署名が正しくても、許可リストに無いアカウントは通さない。"""
        res = self.client.get(
            "/auth/me", headers=self.bearer(make_token(email="stranger@example.com"))
        )
        self.assertEqual(res.status_code, 403)

    def test_expired_token_does_not_fall_back_to_cookie(self):
        """期限切れのトークンを持つ端末が、古い Cookie で通り続けないこと。"""
        self.client.post("/auth/session", json={"access_token": make_token()})
        self.assertEqual(self.client.get("/auth/me").status_code, 200)

        now = int(time.time())
        expired = make_token(exp=now - 10, iat=now - 3600)
        res = self.client.get("/auth/me", headers=self.bearer(expired))
        self.assertEqual(res.status_code, 401)

    def test_session_issues_cookie_for_sse(self):
        res = self.client.post("/auth/session", json={"access_token": make_token()})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"email": ALLOWED_EMAIL})
        self.assertIn(auth.SESSION_COOKIE, self.client.cookies)

        # Authorization を付けなくても Cookie だけで通る（EventSource 用）
        self.assertEqual(self.client.get("/auth/me").status_code, 200)

    def test_session_notifies_login_only_when_event_is_login(self):
        """ログイン通知が飛ぶのは認証コールバック直後だけ。

        Cookie はトークン更新のたびに貼り直されるため、event を見ずに通知すると
        ログインしていないのに通知が飛ぶ。
        """
        sent = []

        async def fake_notify(email, claims, request):
            sent.append((email, claims))

        with patch.object(main, "_notify_login", fake_notify):
            self.client.post("/auth/session", json={"access_token": make_token()})
            self.assertEqual(sent, [], "event 無しで通知が飛んでいる")

            self.client.post(
                "/auth/session", json={"access_token": make_token(), "event": "login"}
            )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ALLOWED_EMAIL)

    def test_login_notification_is_not_sent_for_rejected_account(self):
        """許可されていないアカウントのログインは通知しない。"""
        sent = []

        async def fake_notify(email, claims, request):
            sent.append(email)

        with patch.object(main, "_notify_login", fake_notify):
            res = self.client.post(
                "/auth/session",
                json={"access_token": make_token(email="stranger@example.com"), "event": "login"},
            )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(sent, [])

    def test_session_rejects_invalid_token(self):
        res = self.client.post("/auth/session", json={"access_token": "not-a-jwt"})
        self.assertEqual(res.status_code, 401)
        self.assertNotIn(auth.SESSION_COOKIE, self.client.cookies)

    def test_session_rejects_disallowed_email(self):
        res = self.client.post(
            "/auth/session",
            json={"access_token": make_token(email="stranger@example.com")},
        )
        self.assertEqual(res.status_code, 403)

    def test_logout_clears_cookie(self):
        self.client.post("/auth/session", json={"access_token": make_token()})
        self.client.post("/auth/logout")
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_cookie_of_removed_user_stops_working(self):
        """許可リストから外れたら、発行済み Cookie も通らなくなること。"""
        self.client.post("/auth/session", json={"access_token": make_token()})
        with patch.object(auth, "ALLOWED_EMAILS", set()):
            self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_api_key_still_works(self):
        """スクリプト向けの API キー認証は移行後もそのまま。"""
        key = auth.generate_api_key()
        with patch.object(auth, "_resolve_api_key", lambda presented: "script@example.com"):
            res = self.client.get("/auth/me", headers=self.bearer(key))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"email": "script@example.com"})

    def test_callback_page_is_served(self):
        res = self.client.get("/auth/callback")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])

    def test_old_google_oauth_login_is_gone(self):
        """旧ログイン経路が残っていないこと。"""
        self.assertNotIn("/auth/login", [route.path for route in main.app.routes])


if __name__ == "__main__":
    unittest.main()
