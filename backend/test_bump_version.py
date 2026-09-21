"""バージョン更新スクリプトが「変更内容が空なら更新履歴へエントリを作らない」ことの回帰テスト（#268）。

かつては変更内容が空でも「（変更内容を追記してください）」という仮の文言のエントリを
毎回足していて、誰も埋めないまま更新履歴の画面に残り続けた。`RELEASE_CHANGELOG` を読み、
空ならバージョンだけを上げる。実際のファイルは触らず、一時ディレクトリで動かす。
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"
FRONTEND_CHANGELOG = Path(__file__).resolve().parent.parent / "frontend" / "changelog.js"

PLACEHOLDER = "（変更内容を追記してください）"

CHANGELOG = """'use strict'

const APP_VERSION = '1.0.0'

const APP_CHANGELOG = [
  {
    version: '1.0.0',
    date: '2026-01-01',
    changes: [
      '最初の版',
    ],
  },
]
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bump_version = _load_module()


class ParseReleaseChangelogTest(unittest.TestCase):
    def test_empty_values_give_no_items(self):
        for raw in (None, "", "\n", "  \n \n"):
            self.assertEqual(bump_version.parse_release_changelog(raw), [], repr(raw))

    def test_bullets_and_numbers_are_stripped_one_item_per_line(self):
        raw = "- 一つ目\n* 二つ目\n・三つ目\n1. 四つ目\n2) 五つ目\n\n  六つ目  \n"
        self.assertEqual(
            bump_version.parse_release_changelog(raw),
            ["一つ目", "二つ目", "三つ目", "四つ目", "五つ目", "六つ目"],
        )

    def test_bullet_only_line_is_dropped(self):
        self.assertEqual(bump_version.parse_release_changelog("-\n・"), [])


class InsertChangelogEntryTest(unittest.TestCase):
    def _insert(self, content, changes, version="1.0.1"):
        with redirect_stdout(io.StringIO()) as out:
            result = bump_version.insert_changelog_entry(content, version, "2026-02-02", changes)
        return result, out.getvalue()

    def test_empty_changes_do_not_create_an_entry(self):
        (content, inserted), _ = self._insert(CHANGELOG, [])
        self.assertFalse(inserted)
        self.assertEqual(content, CHANGELOG)
        self.assertNotIn(PLACEHOLDER, content)

    def test_changes_are_added_at_the_top(self):
        (content, inserted), _ = self._insert(CHANGELOG, ["A を直した", "B を追加した"])
        self.assertTrue(inserted)
        marker = bump_version.CHANGELOG_MARKER
        head = content.split(marker, 1)[1]
        self.assertTrue(
            head.startswith(
                "  {\n"
                "    version: '1.0.1',\n"
                "    date: '2026-02-02',\n"
                "    changes: [\n"
                "      'A を直した',\n"
                "      'B を追加した',\n"
                "    ],\n"
                "  },\n"
                "  {\n"
                "    version: '1.0.0',\n"
            ),
            head,
        )

    def test_quotes_and_backslashes_do_not_break_the_js_literal(self):
        (content, _), _ = self._insert(CHANGELOG, ["it's a \\ test"])
        self.assertIn("      'it\\'s a \\\\ test',\n", content)

    def test_missing_marker_still_warns_even_when_changes_are_empty(self):
        broken = "'use strict'\n\nconst APP_VERSION = '1.0.0'\n"
        for changes in ([], ["何か"]):
            (content, inserted), out = self._insert(broken, changes)
            self.assertFalse(inserted)
            self.assertEqual(content, broken)
            self.assertIn("警告: APP_CHANGELOG マーカーが見つかりません", out)

    def test_existing_version_entry_is_left_alone(self):
        (content, inserted), _ = self._insert(CHANGELOG, ["重複"], version="1.0.0")
        self.assertFalse(inserted)
        self.assertEqual(content, CHANGELOG)


class MainTest(unittest.TestCase):
    """`bump_version.py patch` を一時ディレクトリで通しで動かす。"""

    def _run(self, release_changelog):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frontend").mkdir()
            version_file = root / "version.json"
            changelog_file = root / "frontend" / "changelog.js"
            version_file.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
            changelog_file.write_text(CHANGELOG, encoding="utf-8")

            env = {k: v for k, v in os.environ.items() if k != "RELEASE_CHANGELOG"}
            if release_changelog is not None:
                env["RELEASE_CHANGELOG"] = release_changelog

            with (
                mock.patch.object(bump_version, "VERSION_FILE", version_file),
                mock.patch.object(bump_version, "CHANGELOG_FILE", changelog_file),
                mock.patch.object(bump_version, "sync_asset_versions"),
                mock.patch.object(bump_version, "today_jst", return_value="2026-02-02"),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(sys, "argv", ["bump_version.py", "patch"]),
                redirect_stdout(io.StringIO()),
            ):
                bump_version.main()

            version = json.loads(version_file.read_text(encoding="utf-8"))["version"]
            return version, changelog_file.read_text(encoding="utf-8")

    def test_no_release_changelog_bumps_version_without_entry(self):
        for value in (None, "", "\n"):
            version, content = self._run(value)
            self.assertEqual(version, "1.0.1", repr(value))
            self.assertIn("const APP_VERSION = '1.0.1'", content)
            self.assertNotIn("version: '1.0.1'", content)
            self.assertNotIn(PLACEHOLDER, content)

    def test_release_changelog_adds_an_entry(self):
        version, content = self._run("- 通知の表示を直した")
        self.assertEqual(version, "1.0.1")
        self.assertIn("const APP_VERSION = '1.0.1'", content)
        self.assertIn("version: '1.0.1'", content)
        self.assertIn("'通知の表示を直した'", content)

    def test_release_usage_alone_does_not_create_an_entry(self):
        with mock.patch.dict(os.environ, {"RELEASE_USAGE": "1. 設定を開く"}):
            version, content = self._run(None)
        self.assertEqual(version, "1.0.1")
        self.assertNotIn("version: '1.0.1'", content)


class ShippedChangelogTest(unittest.TestCase):
    def test_no_placeholder_entry_remains_in_changelog(self):
        self.assertNotIn(PLACEHOLDER, FRONTEND_CHANGELOG.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
