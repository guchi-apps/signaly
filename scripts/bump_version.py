#!/usr/bin/env python3
"""
バージョンを上げて、変更内容があれば changelog.js にエントリを追加する。

使い方:
  python scripts/bump_version.py patch   # 0.1.0 -> 0.1.1
  python scripts/bump_version.py minor   # 0.1.0 -> 0.2.0
  python scripts/bump_version.py major   # 0.1.0 -> 1.0.0

更新履歴に載せる文面は環境変数 RELEASE_CHANGELOG から受け取る（develop→main のリリース
ワークフローが渡す。改行区切り・箇条書き記号は付いていても外す）。

**RELEASE_CHANGELOG が未設定・空のときは、エントリを作らない**（#268）。かつては
「（変更内容を追記してください）」という仮の文言を毎回入れていたが、誰も埋めないまま
更新履歴の画面に残り続けた。画面で体感できる変化が無い版は履歴に載せず、バージョンだけを上げる。
RELEASE_USAGE（操作手順）だけが渡っても作らない（この画面に手順を出す欄は無い）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python 3.8 fallback

ROOT = Path(__file__).parent.parent
VERSION_FILE = ROOT / "version.json"
CHANGELOG_FILE = ROOT / "frontend" / "changelog.js"
FRONTEND_DIR = ROOT / "frontend"


def sync_asset_versions(version: str) -> None:
    """manifest / HTML / SW の ?v= クエリを揃える。"""
    manifest = FRONTEND_DIR / "manifest.json"
    text = manifest.read_text(encoding="utf-8")
    text = re.sub(r"\?v=[^\"]+", f"?v={version}", text)
    manifest.write_text(text, encoding="utf-8")
    print(f"manifest.json のアイコン URL を v={version} に更新しました。")

    # アセットを増やしたらここへ足す。取りこぼすと、そのファイルだけ古い ?v= のまま残る
    for name in ("index.html", "api-key-docs.html", "auth/callback.html"):
        path = FRONTEND_DIR / name
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"\?v=[0-9.]+", f"?v={version}", text)
        path.write_text(text, encoding="utf-8")
        print(f"{name} の ?v= を v={version} に更新しました。")

    sw = FRONTEND_DIR / "sw.js"
    text = sw.read_text(encoding="utf-8")
    # icon-192.png 決め打ちだと、通知バッジ用の badge-72.png が取り残される（#173）
    text = re.sub(r"(\.png)\?v=[0-9.]+", rf"\1?v={version}", text)
    sw.write_text(text, encoding="utf-8")
    print(f"sw.js のアイコン URL を v={version} に更新しました。")


def bump(version: str, kind: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def today_jst() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")


CHANGELOG_MARKER = "const APP_CHANGELOG = [\n"


def parse_release_changelog(raw: str | None) -> list[str]:
    """RELEASE_CHANGELOG を changes の配列へ整形する。

    生成される文面は箇条書き・段落のどちらもありうるため、行単位に分解し、
    箇条書き記号と番号を落として1行1項目にそろえる。空行は捨てる。
    """
    items = []
    for line in (raw or "").split("\n"):
        item = re.sub(r"^(?:[-*・]|\d+[.)])\s*", "", line.strip()).strip()
        if item:
            items.append(item)
    return items


def _js_string(value: str) -> str:
    """JS の単一引用符リテラルの中身として安全にする。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def insert_changelog_entry(
    content: str, version: str, date: str, changes: list[str]
) -> tuple[str, bool]:
    """APP_CHANGELOG の先頭へエントリを足す。足したかどうかを一緒に返す。

    マーカーが無ければ警告して何も足さない（従来どおり。changelog.js を確認させる）。
    changes が空なら、マーカーの検査のあとで何も足さずに返す。
    """
    if f"version: '{version}'" in content:
        print(f"changelog.js にはすでに v{version} のエントリがあります。スキップします。")
        return content, False

    if CHANGELOG_MARKER not in content:
        print("警告: APP_CHANGELOG マーカーが見つかりません。changelog.js を確認してください。")
        return content, False

    if not changes:
        print(
            f"v{version} には利用者向けの変更内容が無いため、changelog.js へエントリを追加しません。"
        )
        return content, False

    lines = "".join(f"      '{_js_string(change)}',\n" for change in changes)
    entry = (
        f"  {{\n"
        f"    version: '{version}',\n"
        f"    date: '{date}',\n"
        f"    changes: [\n"
        f"{lines}"
        f"    ],\n"
        f"  }},\n"
    )
    print(f"changelog.js に v{version} のエントリを追加しました（{len(changes)}件）。")
    return content.replace(CHANGELOG_MARKER, CHANGELOG_MARKER + entry, 1), True


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("patch", "minor", "major"):
        print("使い方: python scripts/bump_version.py [patch|minor|major]")
        sys.exit(1)

    kind = sys.argv[1]

    data = json.loads(VERSION_FILE.read_text())
    old_version = data["version"]
    new_version = bump(old_version, kind)

    data["version"] = new_version
    VERSION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"version.json: {old_version} -> {new_version}")

    content = CHANGELOG_FILE.read_text(encoding="utf-8")

    # 表示中のバージョン（設定画面など）は、エントリの有無によらず常に新しい版へ揃える
    content = re.sub(
        r"(const APP_VERSION = ')[^']+'",
        f"\\g<1>{new_version}'",
        content,
    )
    content, _ = insert_changelog_entry(
        content,
        new_version,
        today_jst(),
        parse_release_changelog(os.environ.get("RELEASE_CHANGELOG")),
    )

    CHANGELOG_FILE.write_text(content, encoding="utf-8")

    sync_asset_versions(new_version)


if __name__ == "__main__":
    main()
