import unittest
from unittest.mock import AsyncMock, Mock, patch

from login_notify import (
    build_login_notification,
    client_ip,
    send_login_notification,
    user_info_from_claims,
)


class TestClientIp(unittest.TestCase):
    def test_uses_x_forwarded_for(self):
        request = Mock()
        request.headers = {"x-forwarded-for": "203.0.113.1, 10.0.0.1"}
        request.client = Mock(host="127.0.0.1")
        self.assertEqual(client_ip(request), "203.0.113.1")

    def test_falls_back_to_client_host(self):
        request = Mock()
        request.headers = {}
        request.client = Mock(host="192.168.1.5")
        self.assertEqual(client_ip(request), "192.168.1.5")


class TestUserInfoFromClaims(unittest.TestCase):
    def test_picks_only_whitelisted_values(self):
        info = user_info_from_claims({
            "sub": "00000000-0000-4000-8000-000000000001",
            "email": "you@example.com",
            "email_verified": True,
            "session_id": "SHOULD_NOT_APPEAR",
            "user_metadata": {"full_name": "Guchi", "avatar_url": "https://example.com/a.png"},
            "app_metadata": {"provider": "google", "providers": ["google"]},
        })
        self.assertEqual(
            info, {"name": "Guchi", "provider": "google", "verified_email": True}
        )

    def test_tolerates_missing_metadata(self):
        self.assertEqual(
            user_info_from_claims({}),
            {"name": None, "provider": None, "verified_email": None},
        )


class TestBuildLoginNotification(unittest.TestCase):
    def test_includes_core_fields(self):
        request = Mock()
        request.headers = {"user-agent": "Mozilla/5.0 Test", "x-forwarded-for": "1.2.3.4"}
        request.client = None

        result = build_login_notification(
            "user@example.com",
            {"name": "Test User", "provider": "google", "verified_email": True},
            request,
        )

        self.assertEqual(result["title"], "🔐 Signaly ログイン")
        self.assertEqual(result["level"], "info")
        self.assertEqual(result["color"], "#57f287")

        names = [f["name"] for f in result["fields"]]
        self.assertIn("ユーザー", names)
        self.assertIn("メール", names)
        self.assertIn("接続元IP", names)
        self.assertIn("メール確認済", names)
        self.assertIn("プロバイダ", names)
        self.assertIn("User-Agent", names)

        email_field = next(f for f in result["fields"] if f["name"] == "メール")
        self.assertEqual(email_field["value"], "user@example.com")


class TestSendLoginNotification(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_url_unset(self):
        with patch("login_notify.LOGIN_WEBHOOK_URL", ""):
            await send_login_notification({"title": "test"})

    async def test_posts_payload(self):
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        payload = {"title": "🔐 Signaly ログイン", "fields": []}
        with patch("login_notify.LOGIN_WEBHOOK_URL", "https://example.com/webhook/abc"):
            with patch("login_notify.httpx.AsyncClient", return_value=mock_client):
                await send_login_notification(payload)

        mock_client.post.assert_awaited_once_with(
            "https://example.com/webhook/abc",
            json=payload,
        )


if __name__ == "__main__":
    unittest.main()
