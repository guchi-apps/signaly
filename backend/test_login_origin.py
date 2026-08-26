"""見覚えのない接続元からのログインへ警告を付ける処理のテスト（#204）

接続元を覚えたかどうかは行として残るため、戻り値だけでなく実際のテーブルを確かめる。
本番は MySQL だが、ここでは同じ SQLAlchemy モデルを in-memory SQLite に作って
`login_origin.get_session` を差し替える（`database.py` の engine は import 時に接続
しないため、MySQL が無くてもこのファイルは動く）。**StaticPool を省くと接続ごとに空の
DB が作られ、`no such table` で落ちる。**
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import login_origin  # noqa: E402
import main  # noqa: E402
from database import Base, LoginOrigin  # noqa: E402

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def _login_entry(source="Car Care", ip="203.0.113.24", title=None):
    return {
        "id": "n1",
        "channel": "apps-login",
        "title": title if title is not None else f"🔐 {source} ログイン",
        "message": "",
        "level": "info",
        "color": "#57f287",
        "source": source,
        "fields": [
            {"name": "メール", "value": "guchi@example.com", "inline": True},
            {"name": "接続元IP", "value": ip, "inline": True},
            {"name": "日時", "value": "2026-08-25 21:00:00 JST", "inline": False},
        ],
    }


class TestNormalizePrefix(unittest.TestCase):
    def test_ipv4_is_rounded_to_24(self):
        self.assertEqual(login_origin.normalize_prefix("203.0.113.24"), "203.0.113.0/24")
        self.assertEqual(login_origin.normalize_prefix("203.0.113.87"), "203.0.113.0/24")

    def test_ipv6_is_rounded_to_48(self):
        self.assertEqual(
            login_origin.normalize_prefix("2001:db8:1234:5678::1"),
            "2001:db8:1234::/48",
        )

    def test_ignores_note_appended_to_value(self):
        # 警告済みの値をもう一度通しても、IP 部分だけを読む
        self.assertEqual(
            login_origin.normalize_prefix("203.0.113.24 **⚠️ 初めての接続元**"),
            "203.0.113.0/24",
        )

    def test_returns_none_for_non_ip(self):
        self.assertIsNone(login_origin.normalize_prefix("unknown"))
        self.assertIsNone(login_origin.normalize_prefix(""))
        self.assertIsNone(login_origin.normalize_prefix(None))


class TestWarningTitle(unittest.TestCase):
    def test_replaces_lock_emoji(self):
        self.assertEqual(
            login_origin.warning_title("🔐 Car Care ログイン"),
            "⚠️ Car Care ログイン（初めての接続元）",
        )

    def test_is_idempotent(self):
        once = login_origin.warning_title("🔐 Car Care ログイン")
        self.assertEqual(login_origin.warning_title(once), once)

    def test_truncates_to_column_length(self):
        self.assertLessEqual(
            len(login_origin.warning_title("🔐 " + "あ" * 600 + " ログイン")),
            login_origin.MAX_TITLE_LEN,
        )


class LoginOriginDbTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        p = patch.object(login_origin, "get_session", lambda: Session(self.engine))
        p.start()
        self.addCleanup(p.stop)

    def _prefixes(self, scope):
        with Session(self.engine) as session:
            return sorted(
                row.prefix
                for row in session.query(LoginOrigin).filter(LoginOrigin.scope == scope)
            )


class TestRemember(LoginOriginDbTestCase):
    def test_first_record_is_not_a_warning(self):
        self.assertEqual(login_origin.remember("Car Care", "203.0.113.0/24", NOW), "first")
        self.assertEqual(self._prefixes("Car Care"), ["203.0.113.0/24"])

    def test_same_prefix_is_known(self):
        login_origin.remember("Car Care", "203.0.113.0/24", NOW)
        self.assertEqual(login_origin.remember("Car Care", "203.0.113.0/24", NOW), "known")

    def test_unseen_prefix_is_new(self):
        login_origin.remember("Car Care", "203.0.113.0/24", NOW)
        self.assertEqual(login_origin.remember("Car Care", "198.51.100.0/24", NOW), "new")
        self.assertEqual(
            self._prefixes("Car Care"), ["198.51.100.0/24", "203.0.113.0/24"]
        )

    def test_scopes_are_independent(self):
        login_origin.remember("Car Care", "203.0.113.0/24", NOW)
        # 別アプリでは1件目なので警告しない
        self.assertEqual(login_origin.remember("Portfolio", "198.51.100.0/24", NOW), "first")


class TestAnnotate(LoginOriginDbTestCase):
    def test_first_login_stays_green(self):
        entry = _login_entry()
        self.assertEqual(login_origin.annotate(entry), "first")
        self.assertEqual(entry["title"], "🔐 Car Care ログイン")
        self.assertEqual(entry["level"], "info")
        self.assertEqual(entry["color"], "#57f287")

    def test_same_network_stays_green(self):
        login_origin.annotate(_login_entry(ip="203.0.113.24"))
        entry = _login_entry(ip="203.0.113.87")
        self.assertEqual(login_origin.annotate(entry), "known")
        self.assertEqual(entry["color"], "#57f287")

    def test_unseen_network_is_warned(self):
        login_origin.annotate(_login_entry(ip="203.0.113.24"))
        entry = _login_entry(ip="198.51.100.7")
        self.assertEqual(login_origin.annotate(entry), "new")
        self.assertEqual(entry["title"], "⚠️ Car Care ログイン（初めての接続元）")
        self.assertEqual(entry["level"], "warning")
        self.assertEqual(entry["color"], "#fbbf24")

        ip_field = next(f for f in entry["fields"] if f["name"] == "接続元IP")
        self.assertTrue(ip_field["value"].startswith("198.51.100.7"))
        self.assertIn("初めての接続元", ip_field["value"])

    def test_other_fields_are_untouched(self):
        login_origin.annotate(_login_entry(ip="203.0.113.24"))
        entry = _login_entry(ip="198.51.100.7")
        login_origin.annotate(entry)
        self.assertEqual(
            [f["name"] for f in entry["fields"]], ["メール", "接続元IP", "日時"]
        )
        self.assertEqual(entry["fields"][0]["value"], "guchi@example.com")

    def test_non_login_notification_is_skipped(self):
        entry = _login_entry(title="✅ [Signaly] デプロイ 成功")
        self.assertEqual(login_origin.annotate(entry), "skipped")
        self.assertEqual(self._prefixes("Car Care"), [])

    def test_signup_is_not_warned(self):
        # 新規ユーザー登録は接続元が初めてで当たり前なので判定しない
        entry = _login_entry(title="🎉 Car Care 新規ユーザー登録")
        self.assertEqual(login_origin.annotate(entry), "skipped")

    def test_notification_without_ip_is_skipped(self):
        entry = _login_entry()
        entry["fields"] = [{"name": "メール", "value": "guchi@example.com", "inline": True}]
        self.assertEqual(login_origin.annotate(entry), "skipped")

    def test_falls_back_to_channel_when_source_is_missing(self):
        entry = _login_entry()
        entry["source"] = None
        self.assertEqual(login_origin.annotate(entry), "first")
        self.assertEqual(self._prefixes("apps-login"), ["203.0.113.0/24"])

    def test_database_failure_does_not_break_the_notification(self):
        # 照合できなくても、ログインしたという記録そのものは残す
        entry = _login_entry()
        with patch.object(login_origin, "remember", side_effect=RuntimeError("boom")):
            with self.assertLogs(login_origin.logger, level="ERROR"):
                self.assertEqual(login_origin.annotate(entry), "skipped")
        self.assertEqual(entry["title"], "🔐 Car Care ログイン")


class TestWebhookEndpoint(LoginOriginDbTestCase):
    """`POST /webhook/{channel_id}` を通したときに、保存される通知へ警告が乗るか。

    DB と Web Push には触らせない（`_resolve_webhook_target` / `_save_notification` /
    `send_push_notifications` を差し替える）。それ以外は本物のコードを通す。
    """

    CHANNEL_ID = "test-channel-id-0123456"
    CHANNEL_NAME = "apps-login"

    def setUp(self):
        super().setUp()
        self.saved = []
        patches = [
            patch.object(
                main,
                "_resolve_webhook_target",
                lambda cid: (self.CHANNEL_NAME, None) if cid == self.CHANNEL_ID else None,
            ),
            patch.object(main, "_save_notification", self.saved.append),
            patch.object(main, "send_push_notifications", lambda entry: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _post(self, ip):
        payload = _login_entry(ip=ip)
        response = self.client.post(
            f"/webhook/{self.CHANNEL_ID}",
            json={
                "source": payload["source"],
                "title": payload["title"],
                "color": payload["color"],
                "fields": payload["fields"],
            },
        )
        self.assertEqual(response.status_code, 200)
        return self.saved[-1]

    def test_warning_is_stored_for_an_unseen_network(self):
        first = self._post("203.0.113.24")
        self.assertEqual(first["title"], "🔐 Car Care ログイン")
        self.assertEqual(first["level"], "info")

        second = self._post("198.51.100.7")
        self.assertEqual(second["title"], "⚠️ Car Care ログイン（初めての接続元）")
        self.assertEqual(second["level"], "warning")
        self.assertEqual(second["color"], "#fbbf24")

    def test_ci_notification_is_left_alone(self):
        response = self.client.post(
            f"/webhook/{self.CHANNEL_ID}",
            json={"content": "✅ [Signaly] デプロイ 成功"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved[-1]["title"], "✅ [Signaly] デプロイ 成功")
        self.assertEqual(self.saved[-1]["level"], "info")


if __name__ == "__main__":
    unittest.main()
