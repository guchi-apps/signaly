"""見覚えのない接続元からのログインに警告を付ける（#204）

ログイン通知は全アプリで1本のチャンネルへ集まる。数が多いぶん流し読みになるので、
**いつもと違う接続元から届いたものだけ**を黄色にして目立たせる。

判定に使うのは `接続元IP` フィールド（`login_format.FIELD_IP`）だけ。フィールド名が
アプリごとにばらばらだった頃はこの判定ができなかったので、**共通フォーマットへ揃える
ことが前提**になっている。

**完全なIPは保存しない。** 覚えるのは IPv4 なら /24、IPv6 なら /48 に丸めた範囲だけ。
モバイル回線は接続のたびにIPが変わるため、完全一致で覚えると毎回警告になって意味が
なくなる。範囲で覚えれば、回線が変わった初回だけ警告になって以後は落ち着く。

**そのアプリで初めての通知は警告しない。** 記録がまだ1件も無い状態で警告すると、
どのアプリも1回目が必ず黄色になり、警告の意味が薄れる。1件目は覚えるだけにする。
"""

import ipaddress
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database import LoginOrigin, get_session
from login_format import FIELD_IP, field_value

logger = logging.getLogger(__name__)

# 覚える粒度。IPv4 の /24 は同一回線が収まる程度、IPv6 の /48 は一般的な割り当て単位。
IPV4_PREFIX_LEN = 24
IPV6_PREFIX_LEN = 48

WARNING_COLOR = "#fbbf24"  # 黄
WARNING_LEVEL = "warning"
WARNING_TITLE_PREFIX = "⚠️ "
WARNING_TITLE_SUFFIX = "（初めての接続元）"
WARNING_FIELD_NOTE = " **⚠️ 初めての接続元**"

# タイトルは notifications.title（VARCHAR(500)）に入るため、超えないよう切る
MAX_TITLE_LEN = 500

# ログイン通知かどうかの判定。新規ユーザー登録は「初めての接続元」が当たり前なので含めない。
_LOGIN_MARK = "ログイン"

# 判定結果
KNOWN = "known"    # 覚えている範囲
NEW = "new"        # 覚えていない範囲（＝警告する）
FIRST = "first"    # そのアプリで初めての記録（覚えるだけ）
SKIPPED = "skipped"  # 接続元IPが無い・IPとして読めない


def normalize_prefix(value: Any) -> Optional[str]:
    """接続元IPの値を、覚える単位（ネットワーク範囲）の文字列にする。

    値の先頭トークンだけを見る。警告済みの通知を再度通しても壊れないようにするため
    （警告を付けた後の値は `1.2.3.4 **⚠️ …**` のように注記が付く）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    text = text.split()[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    prefix_len = IPV4_PREFIX_LEN if address.version == 4 else IPV6_PREFIX_LEN
    return str(ipaddress.ip_network(f"{address}/{prefix_len}", strict=False))


def is_login_notification(entry: Dict[str, Any]) -> bool:
    return _LOGIN_MARK in str(entry.get("title") or "")


def warning_title(title: str) -> str:
    """タイトルを警告の形にする。プッシュ通知にもこの文字列がそのまま出る。"""
    text = str(title or "").strip()
    for mark in ("⚠️", "🔐", "🎉"):
        if text.startswith(mark):
            text = text[len(mark):].lstrip()
            break
    if not text.endswith(WARNING_TITLE_SUFFIX):
        text = f"{text}{WARNING_TITLE_SUFFIX}"
    return f"{WARNING_TITLE_PREFIX}{text}"[:MAX_TITLE_LEN]


def remember(scope: str, prefix: str, now: Optional[datetime] = None) -> str:
    """接続元を記録し、KNOWN / NEW / FIRST のいずれかを返す。"""
    stamp = now or datetime.now(timezone.utc)
    with get_session() as session:
        row = (
            session.query(LoginOrigin)
            .filter(LoginOrigin.scope == scope, LoginOrigin.prefix == prefix)
            .first()
        )
        if row is not None:
            row.last_seen_at = stamp
            session.commit()
            return KNOWN

        seen_before = (
            session.query(LoginOrigin.id).filter(LoginOrigin.scope == scope).first() is not None
        )
        session.add(
            LoginOrigin(
                id=str(uuid.uuid4()),
                scope=scope,
                prefix=prefix,
                first_seen_at=stamp,
                last_seen_at=stamp,
            )
        )
        try:
            session.commit()
        except Exception:
            # 同じ接続元がほぼ同時に2件届いた場合。既に覚えている扱いにする
            session.rollback()
            return KNOWN
        return NEW if seen_before else FIRST


def annotate(entry: Dict[str, Any]) -> str:
    """ログイン通知なら接続元を照合し、初めての範囲なら entry へ警告を書き込む。

    entry は `_dispatch_notification` が組んだ保存前の通知。戻り値は判定結果
    （呼び出し側はログにしか使わない）。**通知の保存を止めない**——警告を付けられ
    なかったとしても、ログインしたという記録そのものは残す必要がある。
    """
    if not is_login_notification(entry):
        return SKIPPED

    fields = entry.get("fields")
    ip = field_value(fields, FIELD_IP)
    prefix = normalize_prefix(ip)
    if prefix is None:
        return SKIPPED

    # アプリを表すのは送信元。付いていない通知はチャンネル単位で覚える。
    scope = str(entry.get("source") or entry.get("channel") or "").strip()
    if not scope:
        return SKIPPED

    try:
        verdict = remember(scope, prefix)
    except Exception:
        logger.exception("接続元の照合に失敗しました")
        return SKIPPED

    if verdict != NEW:
        return verdict

    entry["title"] = warning_title(entry.get("title") or "")
    entry["level"] = WARNING_LEVEL
    entry["color"] = WARNING_COLOR
    for field in fields:
        if isinstance(field, dict) and str(field.get("name") or "").strip() == FIELD_IP:
            field["value"] = f"{field.get('value')}{WARNING_FIELD_NOTE}"
            break
    return NEW
