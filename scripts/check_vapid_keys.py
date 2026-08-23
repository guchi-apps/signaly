#!/usr/bin/env python3
"""VAPID 公開鍵と秘密鍵のペアを検証する。

使い方:
  # 1Password から読み込む
  op signin
  VAPID_PUBLIC_KEY="$(op read op://apps/signaly/vapid-public-key)" \
  VAPID_PRIVATE_KEY="$(op read op://apps/signaly/vapid-private-key)" \
  python scripts/check_vapid_keys.py

  # 環境変数から読み込む（.env.local 等）
  set -a && source .env.local && set +a
  python scripts/check_vapid_keys.py
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from push import (
    _looks_like_application_server_key,
    _looks_like_truncated_pem,
    _normalize_key_text,
    _parse_private_key,
)


def derive_public_key(private_text: str) -> str:
    key = _parse_private_key(private_text)
    pub_bytes = key.public_key().public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")


def norm_pub(value: str) -> str:
    return (value or "").strip().rstrip("=")


def report(label: str, ok: bool, detail: str = "") -> None:
    mark = "OK" if ok else "NG"
    suffix = f" — {detail}" if detail else ""
    print(f"[{mark}] {label}{suffix}")


def main() -> int:
    public = norm_pub(os.getenv("VAPID_PUBLIC_KEY", ""))
    private_text = _normalize_key_text(os.getenv("VAPID_PRIVATE_KEY", ""))
    subject = (os.getenv("VAPID_SUBJECT", "") or "").strip()
    key_file = (os.getenv("VAPID_PRIVATE_KEY_FILE", "") or "").strip()

    if key_file and not private_text:
        path = Path(key_file)
        if not path.is_file() and not path.is_absolute():
            path = ROOT / "backend" / key_file
        if path.is_file():
            private_text = _normalize_key_text(path.read_text())

    if not public and not private_text:
        print("VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY が未設定です。", file=sys.stderr)
        return 1

    print("=== 形式チェック ===")
    report("vapid-public-key が空でない", bool(public), f"長さ {len(public)}、先頭 {public[:2]}...")
    report(
        "vapid-public-key が Application Server Key 形式",
        _looks_like_application_server_key(public),
    )
    report("vapid-private-key が空でない", bool(private_text), f"長さ {len(private_text)}")
    report(
        "vapid-private-key に BEGIN/END がある",
        "BEGIN" in private_text and "END" in private_text,
    )
    report("vapid-private-key が途中で切れていない", not _looks_like_truncated_pem(private_text))
    report(
        "vapid-private-key が公開鍵と取り違えていない",
        not _looks_like_application_server_key(private_text),
    )
    report(
        "vapid-subject が mailto: 形式",
        subject.lower().startswith("mailto:"),
        subject[:40] + ("..." if len(subject) > 40 else ""),
    )

    print("\n=== 秘密鍵の読み込み ===")
    try:
        derived = derive_public_key(private_text)
        report("秘密鍵 PEM を読み込める", True)
    except Exception as exc:
        report("秘密鍵 PEM を読み込める", False, str(exc)[:160])
        print("\n=== ペア一致 ===")
        report("公開鍵と秘密鍵がペア", False, "秘密鍵を読み込めないため未判定")
        return 1

    print("\n=== ペア一致 ===")
    match = derived == public
    report("公開鍵と秘密鍵がペア", match)
    if not match:
        print(f"  設定されている公開鍵先頭: {public[:12]}...")
        print(f"  秘密鍵から導出した先頭:   {derived[:12]}...")
        print("  → gen_vapid_keys.py で再生成し、3項目をまとめて更新してください")
        return 1

    print("  この組み合わせは正しいペアです。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
