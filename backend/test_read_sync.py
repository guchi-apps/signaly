"""既読を他端末へ伝える `POST /api/read` のテスト（#216）

既読そのものは端末の localStorage が正で、この経路は保存を行わない。確かめるのは
「認証を要求すること」「本人の端末へ配る中継として `send_read_sync` を正しく呼ぶこと」
「通知を出さない Push を無限に増やせないよう入力を絞ること」の3点。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402

EMAIL = "user@example.com"


class ReadSyncEndpointTest(unittest.TestCase):
    def setUp(self):
        main.app.dependency_overrides[auth.require_auth] = lambda: EMAIL
        self.client = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.pop(auth.require_auth, None)

    def test_requires_auth(self):
        main.app.dependency_overrides.pop(auth.require_auth, None)
        res = self.client.post("/api/read", json={"channels": [{"channel": "ci", "until": 1}]})
        self.assertEqual(res.status_code, 401)

    @patch.object(main, "send_read_sync", return_value={"sent": 2, "failed": 0})
    @patch.object(main, "push_configured", return_value=True)
    def test_relays_channels_and_origin_endpoint(self, _configured, send_read_sync):
        res = self.client.post(
            "/api/read",
            json={
                "channels": [
                    {"channel": "ci", "until": 1700000000000},
                    {"channel": "login", "until": 1700000001000},
                ],
                "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "sent": 2, "failed": 0})
        send_read_sync.assert_called_once_with(
            EMAIL,
            [
                {"channel": "ci", "until": 1700000000000},
                {"channel": "login", "until": 1700000001000},
            ],
            "https://fcm.googleapis.com/fcm/send/abc",
        )

    @patch.object(main, "send_read_sync")
    @patch.object(main, "push_configured", return_value=False)
    def test_no_push_config_is_not_an_error(self, _configured, send_read_sync):
        """Push 未設定でも 200 を返す。既読はローカルで完結しており、失敗ではない。"""
        res = self.client.post("/api/read", json={"channels": [{"channel": "ci", "until": 1}]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "sent": 0})
        send_read_sync.assert_not_called()

    @patch.object(main, "send_read_sync")
    @patch.object(main, "push_configured", return_value=True)
    def test_rejects_empty_channels(self, _configured, send_read_sync):
        res = self.client.post("/api/read", json={"channels": []})
        self.assertEqual(res.status_code, 422)
        send_read_sync.assert_not_called()

    @patch.object(main, "send_read_sync")
    @patch.object(main, "push_configured", return_value=True)
    def test_rejects_blank_channel_name(self, _configured, send_read_sync):
        res = self.client.post("/api/read", json={"channels": [{"channel": "  ", "until": 1}]})
        self.assertEqual(res.status_code, 422)
        send_read_sync.assert_not_called()

    @patch.object(main, "send_read_sync")
    @patch.object(main, "push_configured", return_value=True)
    def test_rejects_non_positive_until(self, _configured, send_read_sync):
        res = self.client.post("/api/read", json={"channels": [{"channel": "ci", "until": 0}]})
        self.assertEqual(res.status_code, 422)
        send_read_sync.assert_not_called()

    @patch.object(main, "send_read_sync")
    @patch.object(main, "push_configured", return_value=True)
    def test_rejects_too_many_channels(self, _configured, send_read_sync):
        channels = [{"channel": f"c{i}", "until": 1} for i in range(101)]
        res = self.client.post("/api/read", json={"channels": channels})
        self.assertEqual(res.status_code, 422)
        send_read_sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
