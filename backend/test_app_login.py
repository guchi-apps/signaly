"""app_login.py のユニットテスト"""

import json
import unittest

from app_login import (
    SKIP_DELETE,
    SKIP_NO_SIGN_IN,
    build_fields,
    extract_token,
    parse_app_login_payload,
    skip_reason,
    valid_app_id,
)


def _fields_dict(fields):
    return {f["name"]: f["value"] for f in fields}


# auth.users の実際の行に近いペイロード（機密カラムを含む）
USERS_RECORD = {
    "id": "9f1c4e2a-0000-4000-8000-000000000001",
    "email": "you@example.com",
    "encrypted_password": "$2a$10$SHOULD_NOT_APPEAR",
    "confirmation_token": "conf-SHOULD_NOT_APPEAR",
    "recovery_token": "rec-SHOULD_NOT_APPEAR",
    "email_change_token_new": "chg-SHOULD_NOT_APPEAR",
    "reauthentication_token": "reauth-SHOULD_NOT_APPEAR",
    "email_confirmed_at": "2026-01-01T00:00:00Z",
    "last_sign_in_at": "2026-08-17T10:00:00Z",
    "created_at": "2026-01-01T00:00:00Z",
    "raw_user_meta_data": {"full_name": "Guchi", "avatar_url": "https://example.com/a.png"},
    "raw_app_meta_data": {"provider": "google", "providers": ["google"]},
}

SESSIONS_RECORD = {
    "id": "5c0d0000-0000-4000-8000-000000000002",
    "user_id": "9f1c4e2a-0000-4000-8000-000000000001",
    "created_at": "2026-08-17T10:00:00Z",
    "ip": "203.0.113.10",
    "user_agent": "Mozilla/5.0 (iPhone)",
}


class TestValidAppId(unittest.TestCase):
    def test_ok(self):
        for app_id in ("ops-dashboard", "myroom", "app_1", "a.b-c_d", "a" * 64):
            self.assertTrue(valid_app_id(app_id), app_id)

    def test_ng(self):
        for app_id in ("", "a" * 65, "ops dashboard", "../etc", "日本語", "app/id"):
            self.assertFalse(valid_app_id(app_id), app_id)


class TestExtractToken(unittest.TestCase):
    def test_custom_header(self):
        self.assertEqual(extract_token({"x-signaly-token": "tok1"}), "tok1")

    def test_authorization_bearer(self):
        self.assertEqual(extract_token({"authorization": "Bearer tok2"}), "tok2")

    def test_authorization_bearer_is_case_insensitive(self):
        self.assertEqual(extract_token({"authorization": "bearer tok2"}), "tok2")

    def test_query_param_fallback(self):
        self.assertEqual(extract_token({}, "tok3"), "tok3")

    def test_custom_header_wins(self):
        headers = {"x-signaly-token": "tok1", "authorization": "Bearer tok2"}
        self.assertEqual(extract_token(headers, "tok3"), "tok1")

    def test_missing(self):
        self.assertIsNone(extract_token({}))
        self.assertIsNone(extract_token({"x-signaly-token": "  "}, "  "))

    def test_non_bearer_authorization_is_ignored(self):
        self.assertIsNone(extract_token({"authorization": "Basic abc"}))


class TestBuildFields(unittest.TestCase):
    def test_users_record(self):
        values = _fields_dict(build_fields(USERS_RECORD))
        self.assertEqual(values["ユーザー"], "Guchi")
        self.assertEqual(values["メール"], "you@example.com")
        self.assertEqual(values["プロバイダ"], "google")
        self.assertEqual(values["メール確認済"], "はい")
        self.assertEqual(values["日時"], "2026-08-17 19:00:00 JST")  # UTC の値を JST 表記へ揃える（#204）

    def test_secrets_are_never_exposed(self):
        serialized = json.dumps(build_fields(USERS_RECORD), ensure_ascii=False)
        self.assertNotIn("SHOULD_NOT_APPEAR", serialized)

    def test_nested_objects_are_not_dumped(self):
        serialized = json.dumps(build_fields(USERS_RECORD), ensure_ascii=False)
        # avatar_url / providers など、ホワイトリスト外の入れ子の値は出さない
        self.assertNotIn("avatar_url", serialized)
        self.assertNotIn("providers", serialized)

    def test_sessions_record(self):
        values = _fields_dict(build_fields(SESSIONS_RECORD))
        self.assertEqual(values["接続元IP"], "203.0.113.10")
        self.assertEqual(values["User-Agent"], "Mozilla/5.0 (iPhone)")
        # メールもユーザー名も取れないときだけ ID を出す
        self.assertEqual(values["ユーザーID"], SESSIONS_RECORD["user_id"])
        self.assertEqual(values["日時"], "2026-08-17 19:00:00 JST")  # UTC の値を JST 表記へ揃える（#204）

    def test_user_id_is_omitted_when_identity_is_known(self):
        self.assertNotIn("ユーザーID", _fields_dict(build_fields(USERS_RECORD)))

    def test_email_unconfirmed(self):
        record = dict(USERS_RECORD, email_confirmed_at=None)
        self.assertEqual(_fields_dict(build_fields(record))["メール確認済"], "いいえ")

    def test_long_values_are_truncated(self):
        record = {"user_agent": "x" * 900, "created_at": "2026-08-17T10:00:00Z"}
        self.assertEqual(len(_fields_dict(build_fields(record))["User-Agent"]), 500)

    def test_timestamp_falls_back_to_now(self):
        # last_sign_in_at も created_at も無い場合でも日時は必ず入る
        self.assertIn("日時", _fields_dict(build_fields({"email": "a@example.com"})))

    def test_name_falls_back_through_meta_keys(self):
        record = {"raw_user_meta_data": {"user_name": "guchi-apps"}}
        self.assertEqual(_fields_dict(build_fields(record))["ユーザー"], "guchi-apps")


class TestParsePayload(unittest.TestCase):
    def test_users_update_with_new_sign_in_is_login(self):
        payload = {
            "type": "UPDATE",
            "table": "users",
            "schema": "auth",
            "record": USERS_RECORD,
            "old_record": dict(USERS_RECORD, last_sign_in_at="2026-08-16T10:00:00Z"),
        }
        parsed = parse_app_login_payload("ops-dashboard", payload)
        self.assertEqual(parsed["title"], "🔐 ops-dashboard ログイン")
        self.assertEqual(parsed["color"], "#57f287")
        self.assertEqual(parsed["level"], "info")
        self.assertIsNone(skip_reason(payload))

    def test_users_update_without_new_sign_in_is_skipped(self):
        payload = {
            "type": "UPDATE",
            "table": "users",
            "schema": "auth",
            "record": USERS_RECORD,
            "old_record": dict(USERS_RECORD),
        }
        self.assertIsNone(parse_app_login_payload("ops-dashboard", payload))
        self.assertEqual(skip_reason(payload), SKIP_NO_SIGN_IN)

    def test_users_insert_is_signup(self):
        payload = {
            "type": "INSERT",
            "table": "users",
            "schema": "auth",
            "record": USERS_RECORD,
            "old_record": None,
        }
        parsed = parse_app_login_payload("ops-dashboard", payload)
        self.assertEqual(parsed["title"], "🎉 ops-dashboard 新規ユーザー登録")

    def test_sessions_insert_is_login(self):
        payload = {
            "type": "INSERT",
            "table": "sessions",
            "schema": "auth",
            "record": SESSIONS_RECORD,
            "old_record": None,
        }
        parsed = parse_app_login_payload("ops-dashboard", payload)
        self.assertEqual(parsed["title"], "🔐 ops-dashboard ログイン")
        self.assertEqual(_fields_dict(parsed["fields"])["接続元IP"], "203.0.113.10")

    def test_delete_is_skipped(self):
        payload = {
            "type": "DELETE",
            "table": "users",
            "schema": "auth",
            "record": None,
            "old_record": USERS_RECORD,
        }
        self.assertIsNone(parse_app_login_payload("ops-dashboard", payload))
        self.assertEqual(skip_reason(payload), SKIP_DELETE)

    def test_unknown_table_is_reported_as_event(self):
        payload = {
            "type": "INSERT",
            "table": "profiles",
            "schema": "public",
            "record": {"id": "1", "created_at": "2026-08-17T10:00:00Z"},
            "old_record": None,
        }
        parsed = parse_app_login_payload("myroom", payload)
        self.assertEqual(parsed["title"], "🔔 myroom イベント")
        self.assertEqual(parsed["message"], "public.profiles / INSERT")
        self.assertIsNone(parsed["color"])

    def test_empty_payload_is_reported_as_event(self):
        # 空ボディでも黙って捨てず、設定ミスに気づけるよう通知する
        parsed = parse_app_login_payload("myroom", {})
        self.assertEqual(parsed["title"], "🔔 myroom イベント")

    def test_non_dict_payload_raises(self):
        with self.assertRaises(ValueError):
            parse_app_login_payload("myroom", ["not", "a", "dict"])


if __name__ == "__main__":
    unittest.main()
