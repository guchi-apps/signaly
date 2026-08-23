#!/usr/bin/env python3
"""原本の SVG から PWA / 通知用の PNG アイコンを生成する。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
FRONTEND = ROOT / "frontend"
VERSION = json.loads((ROOT / "version.json").read_text())["version"]

# 用途ごとに原本の SVG が違う。
#   icon.svg       : 角丸タイル。ブラウザのタブ・PWA一覧など「そのまま表示される」用途（purpose:any）
#   icon-full.svg  : 角丸なしの全面塗り。maskable と iOS のホーム画面アイコン用。
#                    maskable はランチャー側が好きな形に切り抜く前提なので、四隅が透過だと
#                    円以外のマスクで角が欠ける。iOS も透過を黒で合成するため不透明にしておく。
#   icon-badge.svg : 前景シルエットのみ・背景透過。Android の通知バッジはアルファだけを
#                    マスクとして使い不透明部分を白で塗り潰すため、地色入りを渡すと塊になる。
OUTPUTS = [
    ("icon.svg", "icon-192.png", 192),
    ("icon.svg", "icon-512.png", 512),
    ("icon-full.svg", "icon-maskable-512.png", 512),
    ("icon-full.svg", "apple-touch-icon.png", 180),
    ("icon-badge.svg", "badge-72.png", 72),
]


def main() -> None:
    for source in {src for src, _, _ in OUTPUTS}:
        svg = FRONTEND / source
        if not svg.is_file():
            print(f"エラー: {svg} が見つかりません", file=sys.stderr)
            sys.exit(1)

    for source, name, size in OUTPUTS:
        out = FRONTEND / name
        subprocess.run(
            [
                "convert",
                "-background",
                "none",
                str(FRONTEND / source),
                "-resize",
                f"{size}x{size}",
                str(out),
            ],
            check=True,
        )
        print(f"生成: {out.name}（{source}）")

    print(f"manifest / HTML のアイコン URL は ?v={VERSION} に合わせてください。")


if __name__ == "__main__":
    main()
