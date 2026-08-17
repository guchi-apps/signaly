"""POST /notify/app-login/{app_id} のエンドポイントテスト

app_login.py の単体テスト（test_app_login.py）では見られない結線を確認する。
- パスパラメータ・ヘッダー・クエリからのトークン取得
- 401 / 400 / 200(skipped) の分岐
- 通知として実際に組み上がった内容（機密が混ざらないこと）

DB と Web Push には触らせない（_fetch_channels / _save_notification /
send_push_notifications を差し替える）。それ以外は本物のコードを通す。
"""

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

CHANNEL_ID = "test-channel-id-0123456"
CHANNEL_NAME = "app-login"

LOGIN_PAYLOAD = {
    "type": "UPDATE",
    "table": "users",
    "schema": "auth",
    "record": {
        "id": "00000000-0000-4000-8000-000000000001",
        "email": "you@example.com",
        "encrypted_password": "$2a$10$SHOULD_NOT_APPEAR",
        "recovery_token": "rec-SHOULD_NOT_APPEAR",
        "email_confirmed_at": "2026-01-01T00:00:00Z",
        "last_sign_in_at": "2026-08-17T10:00:00Z",
        "raw_user_meta_data": {"full_name": "Guchi"},
        "raw_app_meta_data": {"provider": "google"},
    },
    "old_record": {"last_sign_in_at": "2026-08-16T10:00:00Z"},
}


class AppLoginEndpointTest(unittest.TestCase):
    def setUp(self):
        self.saved = []
        patches = [
            patch.object(main, "_fetch_channels", lambda: {CHANNEL_ID: CHANNEL_NAME}),
            patch.object(main, "_save_notification", self.saved.append),
            patch.object(main, "send_push_notifications", lambda entry: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def post(self, path, headers=None, payload=None):
        return self.client.post(
            path,
            headers=headers or {},
            json=LOGIN_PAYLOAD if payload is None else payload,
        )

    # ── 認証 ─────────────────────────────────────────────────────────────

    def test_custom_header_token(self):
        res = self.post("/notify/app-login/ops-dashboard", {"X-Signaly-Token": CHANNEL_ID})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(len(self.saved), 1)

    def test_authorization_bearer_token(self):
        res = self.post(
            "/notify/app-login/ops-dashboard",
            {"Authorization": f"Bearer {CHANNEL_ID}"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.saved), 1)

    def test_query_param_token(self):
        res = self.post(f"/notify/app-login/ops-dashboard?token={CHANNEL_ID}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.saved), 1)

    def test_missing_token_is_401(self):
        res = self.post("/notify/app-login/ops-dashboard")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.saved, [])

    def test_unknown_token_is_401(self):
        res = self.post("/notify/app-login/ops-dashboard", {"X-Signaly-Token": "nope"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.saved, [])

    def test_invalid_app_id_is_400(self):
        res = self.post("/notify/app-login/bad%20id", {"X-Signaly-Token": CHANNEL_ID})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.saved, [])

    def test_invalid_app_id_is_rejected_before_token_check(self):
        # 認証前に弾いても、トークンの有無が app_id の可否から漏れないこと
        res = self.post("/notify/app-login/bad%20id")
        self.assertEqual(res.status_code, 400)

    # ── 通知内容 ─────────────────────────────────────────────────────────

    def test_notification_contents(self):
        res = self.post("/notify/app-login/ops-dashboard", {"X-Signaly-Token": CHANNEL_ID})
        self.assertEqual(res.status_code, 200)

        entry = self.saved[0]
        self.assertEqual(entry["channel"], CHANNEL_NAME)
        self.assertEqual(entry["title"], "🔐 ops-dashboard ログイン")
        self.assertEqual(entry["color"], "#57f287")
        self.assertEqual(res.json()["id"], entry["id"])

        values = {f["name"]: f["value"] for f in entry["fields"]}
        self.assertEqual(values["ユーザー"], "Guchi")
        self.assertEqual(values["メール"], "you@example.com")
        self.assertEqual(values["プロバイダ"], "google")
        self.assertEqual(values["日時"], "2026-08-17T10:00:00Z")

    def test_secrets_never_reach_the_stored_notification(self):
        self.post("/notify/app-login/ops-dashboard", {"X-Signaly-Token": CHANNEL_ID})
        self.assertNotIn("SHOULD_NOT_APPEAR", json.dumps(self.saved[0], ensure_ascii=False))

    # ── 通知しないケース ─────────────────────────────────────────────────

    def test_update_without_new_sign_in_is_skipped(self):
        payload = dict(LOGIN_PAYLOAD, old_record=dict(LOGIN_PAYLOAD["record"]))
        res = self.post(
            "/notify/app-login/ops-dashboard",
            {"X-Signaly-Token": CHANNEL_ID},
            payload,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "skipped": "no_sign_in"})
        self.assertEqual(self.saved, [])

    def test_delete_is_skipped(self):
        payload = {
            "type": "DELETE",
            "table": "users",
            "schema": "auth",
            "record": None,
            "old_record": LOGIN_PAYLOAD["record"],
        }
        res = self.post(
            "/notify/app-login/ops-dashboard",
            {"X-Signaly-Token": CHANNEL_ID},
            payload,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "skipped": "delete"})
        self.assertEqual(self.saved, [])

    # ── ボディ ───────────────────────────────────────────────────────────

    def test_broken_json_is_400(self):
        res = self.client.post(
            "/notify/app-login/ops-dashboard",
            headers={"X-Signaly-Token": CHANNEL_ID, "Content-Type": "application/json"},
            content=b"{not json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.saved, [])

    def test_empty_body_is_reported_as_event(self):
        res = self.client.post(
            "/notify/app-login/ops-dashboard",
            headers={"X-Signaly-Token": CHANNEL_ID, "Content-Type": "application/json"},
            content=b"",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.saved[0]["title"], "🔔 ops-dashboard イベント")

    def test_existing_webhook_endpoint_still_works(self):
        res = self.client.post(
            f"/webhook/{CHANNEL_ID}",
            json={"content": "既存の Discord 形式"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.saved[0]["title"], "既存の Discord 形式")


if __name__ == "__main__":
    unittest.main()
