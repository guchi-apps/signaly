"""push.py のユニットテスト"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from push import (
    _build_payload,
    _looks_like_application_server_key,
    _looks_like_truncated_pem,
    _normalize_key_text,
    _parse_private_key,
    push_vapid_healthy,
    send_push_notifications,
    send_read_sync,
    send_test_push_to_user,
    VAPID_SUBJECT,
)


def _sample_private_key():
    return ec.generate_private_key(ec.SECP256R1())


def _sample_pkcs8_pem(key=None) -> str:
    key = key or _sample_private_key()
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()


def _sample_ec_pem(key=None) -> str:
    key = key or _sample_private_key()
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    ).decode()


class TestPrivateKeyParsing(unittest.TestCase):
    def test_normalize_key_text_unescapes_newlines(self):
        pem = _sample_pkcs8_pem()
        escaped = pem.replace("\n", "\\n")
        self.assertIn("\n", _normalize_key_text(escaped))
        self.assertNotIn("\\n", _normalize_key_text(escaped))

    def test_parse_pkcs8_pem(self):
        pem = _sample_pkcs8_pem()
        key = _parse_private_key(pem)
        self.assertEqual(key.key_size, 256)

    def test_parse_ec_pem(self):
        pem = _sample_ec_pem()
        key = _parse_private_key(pem)
        self.assertEqual(key.key_size, 256)

    def test_parse_escaped_one_line_pem(self):
        pem = _sample_pkcs8_pem().replace("\n", "\\n")
        key = _parse_private_key(_normalize_key_text(pem))
        self.assertEqual(key.key_size, 256)

    def test_parse_pkcs8_der_without_headers(self):
        key = _sample_private_key()
        der_b64 = base64.b64encode(
            key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
        ).decode()
        parsed = _parse_private_key(der_b64)
        self.assertEqual(parsed.key_size, 256)

    def test_rejects_application_server_key(self):
        key = _sample_private_key()
        pub_bytes = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        app_key = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")
        self.assertTrue(_looks_like_application_server_key(app_key))
        with self.assertRaisesRegex(ValueError, "公開鍵"):
            _parse_private_key(app_key)

    def test_detects_truncated_pem(self):
        self.assertTrue(_looks_like_truncated_pem("-----BEGIN PRIVATE KEY-----"))
        self.assertFalse(_looks_like_truncated_pem(_sample_pkcs8_pem()))

    def test_vapid_private_key_text_from_file(self):
        pem = _sample_pkcs8_pem()
        with tempfile.TemporaryDirectory() as tmp:
            pem_path = Path(tmp) / "vapid_private.pem"
            pem_path.write_text(pem)
            import push as push_module

            with patch.object(push_module, "VAPID_PRIVATE_KEY", ""), patch.object(
                push_module, "VAPID_PRIVATE_KEY_FILE", str(pem_path)
            ):
                text = push_module._vapid_private_key_text()
                key = push_module._parse_private_key(text)
                self.assertEqual(key.key_size, 256)


class TestVapidHelpers(unittest.TestCase):
    @patch("push.push_configured", return_value=False)
    def test_push_vapid_healthy_not_configured(self, _configured):
        ok, err = push_vapid_healthy()
        self.assertFalse(ok)
        self.assertEqual(err, "not_configured")

    @patch("push._load_vapid")
    @patch("push.push_configured", return_value=True)
    def test_push_vapid_healthy_ok(self, _configured, _load):
        ok, err = push_vapid_healthy()
        self.assertTrue(ok)
        self.assertIsNone(err)
        _load.assert_called_once()


class TestSendPushNotifications(unittest.TestCase):
    @patch("push.resolve_notification_enabled", return_value=True)
    @patch("push.webpush")
    @patch("push._load_vapid")
    @patch("push._fetch_subscriptions")
    @patch("push.push_configured", return_value=True)
    def test_vapid_claims_are_not_reused_across_subscriptions(
        self, _configured, fetch_subs, _load_vapid, webpush, _resolve
    ):
        """各端末へ渡す vapid_claims が別オブジェクトであること"""
        fetch_subs.return_value = [
            {
                "id": "1",
                "email": "user@example.com",
                "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                "p256dh": "k1",
                "auth": "a1",
            },
            {
                "id": "2",
                "email": "blocked@example.com",
                "endpoint": "https://web.push.apple.com/xyz",
                "p256dh": "k2",
                "auth": "a2",
            },
        ]

        captured = []

        def capture_claims(**kwargs):
            captured.append(dict(kwargs["vapid_claims"]))

        webpush.side_effect = capture_claims

        send_push_notifications(
            {
                "id": "n1",
                "channel": "test",
                "title": "t",
                "message": "m",
                "level": "info",
                "fields": [],
            }
        )

        self.assertEqual(webpush.call_count, 2)
        self.assertEqual(len(captured), 2)
        self.assertIsNot(captured[0], captured[1])
        self.assertEqual(captured[0]["sub"], VAPID_SUBJECT)
        self.assertEqual(captured[0]["aud"], "https://fcm.googleapis.com")
        self.assertEqual(captured[1]["aud"], "https://web.push.apple.com")

    @patch("push.resolve_notification_enabled")
    @patch("push.webpush")
    @patch("push._load_vapid")
    @patch("push._fetch_subscriptions")
    @patch("push.push_configured", return_value=True)
    def test_skips_disabled_users(
        self, _configured, fetch_subs, _load_vapid, webpush, resolve_enabled
    ):
        fetch_subs.return_value = [
            {
                "id": "1",
                "email": "allowed@example.com",
                "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                "p256dh": "k1",
                "auth": "a1",
            },
            {
                "id": "2",
                "email": "blocked@example.com",
                "endpoint": "https://web.push.apple.com/xyz",
                "p256dh": "k2",
                "auth": "a2",
            },
        ]

        def enabled_for(email, _channel):
            return email != "blocked@example.com"

        resolve_enabled.side_effect = enabled_for

        send_push_notifications(
            {
                "id": "n1",
                "channel": "test",
                "title": "t",
                "message": "m",
                "level": "info",
                "fields": [],
            }
        )

        self.assertEqual(webpush.call_count, 1)


class TestSendTestPushToUser(unittest.TestCase):
    @patch("push.push_configured", return_value=False)
    def test_not_configured(self, _configured):
        result = send_test_push_to_user("user@example.com")
        self.assertEqual(result["error"], "not_configured")

    @patch("push.push_configured", return_value=True)
    @patch("push._fetch_subscriptions_for_email", return_value=[])
    def test_no_subscription(self, _fetch, _configured):
        result = send_test_push_to_user("user@example.com")
        self.assertEqual(result["error"], "no_subscription")

    @patch("push.webpush")
    @patch("push._load_vapid")
    @patch("push._fetch_subscriptions_for_email")
    @patch("push.push_configured", return_value=True)
    def test_sends_to_user_subscriptions(self, _configured, fetch_subs, load_vapid, webpush):
        vapid = object()
        load_vapid.return_value = vapid
        fetch_subs.return_value = [
            {
                "id": "1",
                "email": "user@example.com",
                "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                "p256dh": "k1",
                "auth": "a1",
            },
        ]
        result = send_test_push_to_user("user@example.com")
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(webpush.call_count, 1)
        self.assertIs(webpush.call_args.kwargs["vapid_private_key"], vapid)

    @patch("push.webpush")
    @patch("push._load_vapid")
    @patch("push._fetch_subscriptions_for_email")
    @patch("push.push_configured", return_value=True)
    def test_filters_by_endpoint(self, _configured, fetch_subs, _load_vapid, webpush):
        fetch_subs.return_value = [
            {
                "id": "1",
                "email": "user@example.com",
                "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                "p256dh": "k1",
                "auth": "a1",
            },
            {
                "id": "2",
                "email": "user@example.com",
                "endpoint": "https://web.push.apple.com/xyz",
                "p256dh": "k2",
                "auth": "a2",
            },
        ]
        result = send_test_push_to_user(
            "user@example.com",
            "https://web.push.apple.com/xyz",
        )
        self.assertEqual(result["sent"], 1)
        self.assertEqual(webpush.call_count, 1)


class TestSendReadSync(unittest.TestCase):
    """既読同期（#216）は本人の端末だけへ、送信元を除いて配る"""

    SUBS = [
        {
            "id": "1",
            "email": "user@example.com",
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "p256dh": "k1",
            "auth": "a1",
        },
        {
            "id": "2",
            "email": "user@example.com",
            "endpoint": "https://web.push.apple.com/xyz",
            "p256dh": "k2",
            "auth": "a2",
        },
    ]

    @patch("push.push_configured", return_value=False)
    def test_not_configured(self, _configured):
        result = send_read_sync("user@example.com", [{"channel": "ci", "until": 1}])
        self.assertEqual(result["error"], "not_configured")

    @patch("push.webpush")
    @patch("push._load_vapid")
    @patch("push._fetch_subscriptions_for_email")
    @patch("push.push_configured", return_value=True)
    def test_no_channels_sends_nothing(self, _configured, fetch_subs, _load_vapid, webpush):
        result = send_read_sync("user@example.com", [])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(webpush.call_count, 0)
        self.assertEqual(fetch_subs.call_count, 0)

    @patch("push.webpush")
    @patch("push._load_vapid")
    @patch("push._fetch_subscriptions_for_email")
    @patch("push.push_configured", return_value=True)
    def test_excludes_origin_endpoint(self, _configured, fetch_subs, _load_vapid, webpush):
        fetch_subs.return_value = list(self.SUBS)
        result = send_read_sync(
            "user@example.com",
            [{"channel": "ci", "until": 1700000000000}],
            "https://fcm.googleapis.com/fcm/send/abc",
        )
        self.assertEqual(result["sent"], 1)
        self.assertEqual(webpush.call_count, 1)
        fetch_subs.assert_called_once_with("user@example.com")
        sent_to = webpush.call_args.kwargs["subscription_info"]["endpoint"]
        self.assertEqual(sent_to, "https://web.push.apple.com/xyz")

    @patch("push.webpush")
    @patch("push._load_vapid")
    @patch("push._fetch_subscriptions_for_email")
    @patch("push.push_configured", return_value=True)
    def test_payload_carries_channels_and_short_ttl(
        self, _configured, fetch_subs, _load_vapid, webpush
    ):
        fetch_subs.return_value = [dict(self.SUBS[0])]
        channels = [
            {"channel": "ci", "until": 1700000000000},
            {"channel": "login", "until": 1700000001000},
        ]
        send_read_sync("user@example.com", channels)
        payload = json.loads(webpush.call_args.kwargs["data"])
        self.assertEqual(payload["type"], "read")
        self.assertEqual(payload["channels"], channels)
        # 遅れて届いた既読同期が、その間の新着まで消さないよう TTL は短く
        self.assertEqual(webpush.call_args.kwargs["ttl"], 60)


class TestBuildPayload(unittest.TestCase):
    def test_includes_timestamp_for_read_sync(self):
        payload = json.loads(
            _build_payload(
                {
                    "id": "n1",
                    "channel": "ci",
                    "title": "t",
                    "message": "m",
                    "timestamp": "2026-08-27T00:00:00Z",
                }
            )
        )
        self.assertEqual(payload["ts"], "2026-08-27T00:00:00Z")
        self.assertEqual(payload["channel"], "ci")

    def test_timestamp_missing_becomes_empty(self):
        payload = json.loads(_build_payload({"id": "n1", "channel": "ci", "message": "m"}))
        self.assertEqual(payload["ts"], "")


if __name__ == "__main__":
    unittest.main()
