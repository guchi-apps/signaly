"""webhook.py のユニットテスト"""

import unittest

from webhook import (
    discord_color_to_hex,
    is_discord_payload,
    normalize_source,
    parse_discord_payload,
    parse_legacy_payload,
    parse_webhook_payload,
)


class TestDiscordColor(unittest.TestCase):
    def test_green(self):
        self.assertEqual(discord_color_to_hex(5763719), "#57f287")

    def test_red(self):
        self.assertEqual(discord_color_to_hex(15548997), "#ed4245")

    def test_none(self):
        self.assertIsNone(discord_color_to_hex(None))


class TestFormatDetection(unittest.TestCase):
    def test_discord_content(self):
        self.assertTrue(is_discord_payload({"content": "hi"}))

    def test_discord_embeds(self):
        self.assertTrue(is_discord_payload({"embeds": []}))

    def test_legacy(self):
        self.assertFalse(is_discord_payload({"title": "x", "message": "y"}))


class TestParseDiscord(unittest.TestCase):
    def test_content_only(self):
        result = parse_discord_payload({"content": "Hello"})
        self.assertEqual(result["title"], "Hello")
        self.assertEqual(result["message"], "")

    def test_embed_with_fields(self):
        result = parse_discord_payload({
            "embeds": [{
                "title": "CI Success",
                "description": "All green",
                "color": 5763719,
                "fields": [{"name": "Branch", "value": "main", "inline": True}],
            }],
        })
        self.assertEqual(result["title"], "CI Success")
        self.assertEqual(result["message"], "All green")
        self.assertEqual(result["color"], "#57f287")
        self.assertEqual(len(result["fields"]), 1)

    def test_content_and_embed(self):
        result = parse_discord_payload({
            "content": "Ping",
            "embeds": [{"title": "Alert", "description": "Details"}],
        })
        self.assertEqual(result["title"], "Alert")
        self.assertEqual(result["message"], "Ping\n\nDetails")

    def test_multiline_content_splits_title(self):
        result = parse_discord_payload({
            "content": ":rocket: **SSHログイン通知**\n**サーバー:** myserver\n**ユーザー:** root",
        })
        self.assertEqual(result["title"], ":rocket: **SSHログイン通知**")
        self.assertEqual(result["message"], "**サーバー:** myserver\n**ユーザー:** root")
        self.assertIsNone(result["fields"])

    def test_literal_backslash_n(self):
        result = parse_discord_payload({
            "content": "🚀 **SSHログイン通知**\\n**サーバー:** myserver",
        })
        self.assertEqual(result["title"], "🚀 **SSHログイン通知**")
        self.assertEqual(result["message"], "**サーバー:** myserver")

    def test_single_line_content_becomes_title(self):
        result = parse_discord_payload({
            "content": "Hello",
        })
        self.assertEqual(result["title"], "Hello")
        self.assertEqual(result["message"], "")

    def test_username_fallback(self):
        result = parse_discord_payload({"username": "Bot"})
        self.assertEqual(result["title"], "Bot")

    def test_embed_url_title(self):
        result = parse_discord_payload({
            "embeds": [{"title": "Build", "url": "https://example.com/build/1"}],
        })
        self.assertEqual(result["title"], "[Build](https://example.com/build/1)")


class TestParseLegacy(unittest.TestCase):
    def test_defaults(self):
        result = parse_legacy_payload({})
        self.assertEqual(result["level"], "info")
        self.assertEqual(result["message"], "")

    def test_full(self):
        result = parse_legacy_payload({
            "title": "T",
            "message": "M",
            "level": "error",
            "color": "#ff0000",
            "fields": [{"name": "A", "value": "B"}],
        })
        self.assertEqual(result["level"], "error")
        self.assertEqual(result["color"], "#ff0000")


class TestParseWebhook(unittest.TestCase):
    def test_routes_discord(self):
        result = parse_webhook_payload({"content": "x"})
        self.assertEqual(result["title"], "x")
        self.assertEqual(result["message"], "")

    def test_routes_legacy(self):
        result = parse_webhook_payload({"message": "legacy"})
        self.assertEqual(result["message"], "legacy")


class TestNormalizeSource(unittest.TestCase):
    def test_trims(self):
        self.assertEqual(normalize_source("  signaly  "), "signaly")

    def test_empty_is_none(self):
        self.assertIsNone(normalize_source("   "))
        self.assertIsNone(normalize_source(None))

    def test_rejects_non_scalar(self):
        self.assertIsNone(normalize_source({"a": 1}))
        self.assertIsNone(normalize_source(["a"]))
        self.assertIsNone(normalize_source(True))

    def test_truncates(self):
        self.assertEqual(len(normalize_source("x" * 200)), 100)


class TestSourceDetection(unittest.TestCase):
    """送信元は既存ペイロードから拾えること（送信側の変更を要らなくするため）"""

    def test_ci_payload_app_field(self):
        # .github/scripts/signaly-notify.sh が実際に送る形
        result = parse_webhook_payload({
            "title": "✅ [Signaly] デプロイ 成功",
            "color": "#57f287",
            "fields": [
                {"name": "App", "value": "Signaly", "inline": True},
                {"name": "Repository", "value": "`guchi-apps/signaly`", "inline": True},
            ],
        })
        self.assertEqual(result["source"], "Signaly")

    def test_repository_field_fallback(self):
        result = parse_webhook_payload({
            "title": "CI",
            "fields": [{"name": "Repository", "value": "`guchi-apps/car-care`", "inline": True}],
        })
        self.assertEqual(result["source"], "car-care")

    def test_explicit_source_wins(self):
        result = parse_webhook_payload({
            "source": "手動",
            "fields": [{"name": "App", "value": "Signaly"}],
        })
        self.assertEqual(result["source"], "手動")

    def test_discord_username_fallback(self):
        result = parse_discord_payload({"content": "hi", "username": "Grafana"})
        self.assertEqual(result["source"], "Grafana")

    def test_discord_fields_beat_username(self):
        result = parse_discord_payload({
            "username": "GitHub",
            "embeds": [{"title": "CI", "fields": [{"name": "App", "value": "issue-deck"}]}],
        })
        self.assertEqual(result["source"], "issue-deck")

    def test_no_source(self):
        self.assertIsNone(parse_webhook_payload({"message": "plain"})["source"])
        self.assertIsNone(parse_discord_payload({"content": "plain"})["source"])

    def test_legacy_fields_not_a_list(self):
        # fields が想定外の型でも落ちない
        self.assertIsNone(parse_legacy_payload({"message": "x", "fields": "bad"})["source"])


if __name__ == "__main__":
    unittest.main()
