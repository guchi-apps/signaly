"""ログイン通知の共通フォーマットのテスト（#204）

`docs/webhook.md` に「正」として書いた形と、このモジュールが実際に組む形がずれると、
各アプリのテンプレートだけが古いまま残る。並び・名前・日時の書式をここで固定する。
"""

import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("DB_NAME", "ci_signaly")

import login_format  # noqa: E402


def _names(fields):
    return [f["name"] for f in fields]


def _values(fields):
    return {f["name"]: f["value"] for f in fields}


class TestFormatTimestamp(unittest.TestCase):
    def test_utc_iso_becomes_jst(self):
        self.assertEqual(
            login_format.format_timestamp("2026-08-17T10:00:00Z"),
            "2026-08-17 19:00:00 JST",
        )

    def test_offset_iso_becomes_jst(self):
        self.assertEqual(
            login_format.format_timestamp("2026-08-17T19:00:00+09:00"),
            "2026-08-17 19:00:00 JST",
        )

    def test_naive_datetime_is_treated_as_utc(self):
        self.assertEqual(
            login_format.format_timestamp(datetime(2026, 8, 17, 10, 0, 0)),
            "2026-08-17 19:00:00 JST",
        )

    def test_aware_datetime(self):
        self.assertEqual(
            login_format.format_timestamp(datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)),
            "2026-08-17 19:00:00 JST",
        )

    def test_unparsable_value_is_kept_as_is(self):
        # 分からない値を勝手に別の時刻へ読み替えない
        self.assertEqual(login_format.format_timestamp("先ほど"), "先ほど")

    def test_none_uses_now(self):
        self.assertTrue(login_format.format_timestamp().endswith(" JST"))


class TestBuildFields(unittest.TestCase):
    def test_order_is_fixed(self):
        fields = login_format.build_fields(
            user="guchi",
            email="guchi@example.com",
            provider="google",
            ip="203.0.113.24",
            email_verified=True,
            timestamp="2026-08-25T05:03:22Z",
            user_agent="Mozilla/5.0",
        )
        self.assertEqual(
            _names(fields),
            ["ユーザー", "メール", "プロバイダ", "接続元IP", "メール確認済", "日時", "User-Agent"],
        )

    def test_inline_flags(self):
        fields = login_format.build_fields(user="guchi", ip="203.0.113.24", user_agent="Mozilla/5.0")
        inline = {f["name"]: f["inline"] for f in fields}
        self.assertTrue(inline["ユーザー"])
        self.assertTrue(inline["接続元IP"])
        self.assertFalse(inline["日時"])
        self.assertFalse(inline["User-Agent"])

    def test_missing_values_are_dropped_not_filled_with_unknown(self):
        fields = login_format.build_fields(ip="203.0.113.24")
        self.assertEqual(_names(fields), ["接続元IP", "日時"])

    def test_timestamp_is_always_present(self):
        self.assertIn("日時", _names(login_format.build_fields()))

    def test_boolean_verified_is_japanese(self):
        self.assertEqual(_values(login_format.build_fields(email_verified=False))["メール確認済"], "いいえ")

    def test_user_id_only_when_identity_is_unknown(self):
        with_identity = login_format.build_fields(email="a@example.com", user_id="uid-1")
        self.assertNotIn("ユーザーID", _names(with_identity))

        without_identity = login_format.build_fields(user_id="uid-1")
        self.assertIn("ユーザーID", _names(without_identity))

    def test_long_values_are_truncated(self):
        fields = login_format.build_fields(user_agent="x" * 900)
        self.assertEqual(len(_values(fields)["User-Agent"]), login_format.MAX_VALUE_LEN)

    def test_nested_values_are_never_dumped(self):
        fields = login_format.build_fields(user={"full_name": "guchi"}, ip=["1.2.3.4"])
        self.assertEqual(_names(fields), ["日時"])


class TestBuildPayload(unittest.TestCase):
    def test_login(self):
        payload = login_format.build_payload("Car Care", ip="203.0.113.24")
        self.assertEqual(payload["title"], "🔐 Car Care ログイン")
        self.assertEqual(payload["color"], "#57f287")
        self.assertEqual(payload["level"], "info")
        self.assertEqual(payload["message"], "")
        # 用途別の1チャンネルへ集約しているため、送信元は必ず載せる
        self.assertEqual(payload["source"], "Car Care")

    def test_signup(self):
        payload = login_format.build_payload("Car Care", login_format.KIND_SIGNUP)
        self.assertEqual(payload["title"], "🎉 Car Care 新規ユーザー登録")
        self.assertEqual(payload["color"], "#fbbf24")


class TestFieldValue(unittest.TestCase):
    def test_finds_by_name(self):
        fields = login_format.build_fields(ip="203.0.113.24")
        self.assertEqual(login_format.field_value(fields, "接続元IP"), "203.0.113.24")

    def test_returns_none_when_absent(self):
        self.assertIsNone(login_format.field_value([], "接続元IP"))
        self.assertIsNone(login_format.field_value(None, "接続元IP"))


if __name__ == "__main__":
    unittest.main()
