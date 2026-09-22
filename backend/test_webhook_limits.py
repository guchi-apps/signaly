"""Webhook 入力の型・長さ不備で 500 にならないことのエンドポイントテスト（#280）

`notifications.title` は VARCHAR(500)、`level` / `color` は VARCHAR(20) だが、
webhook 入口ではこれらの長さ・型を検証していなかった。MySQL の strict mode では
列幅を超える INSERT が DataError になり、`_save_notification` が例外を出して
通知そのものが保存されなくなる。ここでは実際に `POST /webhook/{channel_id}` を
通し、200 が返ること・保存される値が列幅に収まっていることを確認する。

DB へは触らせない（`_resolve_webhook_target` / `_save_notification` /
`send_push_notifications` を差し替える）。それ以外は本物のコードを通す
（backend/test_login_origin.py と同じ方式）。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from database import Notification  # noqa: E402

# database.py の列幅と一致させる
MAX_TITLE_LEN = Notification.__table__.c.title.type.length
MAX_LEVEL_LEN = Notification.__table__.c.level.type.length
MAX_COLOR_LEN = Notification.__table__.c.color.type.length

CHANNEL_ID = "test-channel-id-0123456"
CHANNEL_NAME = "general"


class WebhookLimitsTest(unittest.TestCase):
    def setUp(self):
        self.saved = []
        patches = [
            patch.object(
                main,
                "_resolve_webhook_target",
                lambda cid: (CHANNEL_NAME, None) if cid == CHANNEL_ID else None,
            ),
            patch.object(main, "_save_notification", self.saved.append),
            patch.object(main, "send_push_notifications", lambda entry: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _post(self, payload):
        response = self.client.post(f"/webhook/{CHANNEL_ID}", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return self.saved[-1]

    def test_oversized_legacy_title_does_not_500(self):
        entry = self._post({"title": "x" * 1000, "message": "m", "level": "info"})
        self.assertLessEqual(len(entry["title"]), MAX_TITLE_LEN)

    def test_oversized_discord_embed_title_does_not_500(self):
        entry = self._post({
            "embeds": [{"title": "x" * 1000, "url": "https://example.com"}],
        })
        self.assertLessEqual(len(entry["title"]), MAX_TITLE_LEN)

    def test_unexpected_level_value_does_not_500(self):
        entry = self._post({"message": "m", "level": "critical-x" * 5})
        self.assertLessEqual(len(entry["level"]), MAX_LEVEL_LEN)
        self.assertEqual(entry["level"], "info")

    def test_oversized_color_does_not_500(self):
        entry = self._post({"message": "m", "color": "#" + "f" * 100})
        self.assertLessEqual(len(entry["color"]), MAX_COLOR_LEN)

    def test_non_string_content_does_not_500(self):
        entry = self._post({"content": {"unexpected": "object"}})
        self.assertEqual(entry["title"], "")
        self.assertEqual(entry["message"], "")

    def test_non_string_embed_description_does_not_500(self):
        entry = self._post({
            "embeds": [{"title": "T", "description": [1, 2, 3]}],
        })
        self.assertEqual(entry["title"], "T")
        self.assertEqual(entry["message"], "")


if __name__ == "__main__":
    unittest.main()
