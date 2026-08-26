"""ログイン通知の共通フォーマット（#204）

**この形の正は `docs/webhook.md`。** ログイン通知は全アプリで1本のチャンネルへ集約して
いるため、送る側がばらばらの形で送ると、同じ種類の通知に見えない。各アプリはこの形へ
揃えて送る（Next.js 用のコピー元テンプレートも `docs/webhook.md` にある）。

このモジュールは Signaly 自身が送る側になる2経路——`login_notify.py`（Signaly への
ログイン）と `app_login.py`（Supabase Database Webhooks 経由の他アプリのログイン）
——で共有する。**受信側では使わない。** 受け取った通知を整え直すことはせず、
届いたものはそのまま保存する（整形の定義が送信側と受信側の2か所に散るのを避ける）。

**フィールド名を変えないこと。** `接続元IP` は受信側（`login_origin.py`）が
「見覚えのない接続元か」を判定するために名前で引いている。名前を変えると警告が
黙って効かなくなる。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

JST = timezone(timedelta(hours=9))

# 日時フィールドの書式。JST 固定にしているのは、送る側が UTC と JST で割れていて
# 並べたときに前後関係が読めなかったため（#204）。
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S JST"

KIND_LOGIN = "login"
KIND_SIGNUP = "signup"

COLOR_LOGIN = "#57f287"   # 緑
COLOR_SIGNUP = "#fbbf24"  # 黄

MAX_VALUE_LEN = 500

FIELD_USER = "ユーザー"
FIELD_EMAIL = "メール"
FIELD_PROVIDER = "プロバイダ"
FIELD_IP = "接続元IP"
FIELD_VERIFIED = "メール確認済"
FIELD_USER_ID = "ユーザーID"
FIELD_TIMESTAMP = "日時"
FIELD_USER_AGENT = "User-Agent"

# 横並び（inline）で出す項目の並び順。ここに無いものは下段に1行で出す。
_INLINE_ORDER = (
    FIELD_USER,
    FIELD_EMAIL,
    FIELD_PROVIDER,
    FIELD_IP,
    FIELD_VERIFIED,
    FIELD_USER_ID,
)


def _text(value: Any) -> Optional[str]:
    """通知に載せてよいスカラー値だけを文字列化する。dict / list は載せない。"""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    text = str(value).strip()
    if not text:
        return None
    return text[:MAX_VALUE_LEN]


def _parse_datetime(value: Any) -> Optional[datetime]:
    """datetime か ISO 8601 文字列を aware な datetime にする。解釈できなければ None。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # タイムゾーンが無い値は UTC とみなす（Supabase の `last_sign_in_at` はUTC）
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_timestamp(value: Any = None) -> str:
    """日時フィールドの値を作る。

    `value` が None なら現在時刻。解釈できない文字列はそのまま返す
    （分からない値を勝手に別の時刻へ読み替えない）。
    """
    if value is None:
        return datetime.now(JST).strftime(TIMESTAMP_FORMAT)
    parsed = _parse_datetime(value)
    if parsed is None:
        return _text(value) or datetime.now(JST).strftime(TIMESTAMP_FORMAT)
    return parsed.astimezone(JST).strftime(TIMESTAMP_FORMAT)


def build_fields(
    *,
    user: Any = None,
    email: Any = None,
    provider: Any = None,
    ip: Any = None,
    email_verified: Any = None,
    user_id: Any = None,
    timestamp: Any = None,
    user_agent: Any = None,
) -> List[dict]:
    """共通フォーマットのフィールドを、決まった並びで組む。

    値が取れない項目はフィールドごと落とす。「不明」を並べると、どのアプリでも
    同じ行数になる代わりに、実際に取れている情報が読み取れなくなるため。
    """
    values = {
        FIELD_USER: _text(user),
        FIELD_EMAIL: _text(email),
        FIELD_PROVIDER: _text(provider),
        FIELD_IP: _text(ip),
        FIELD_VERIFIED: _text(email_verified),
        # メールも表示名も取れないとき（auth.sessions 起点）だけ ID を出す
        FIELD_USER_ID: _text(user_id) if not _text(email) and not _text(user) else None,
    }

    fields = [
        {"name": name, "value": values[name], "inline": True}
        for name in _INLINE_ORDER
        if values[name] is not None
    ]

    fields.append({"name": FIELD_TIMESTAMP, "value": format_timestamp(timestamp), "inline": False})

    ua = _text(user_agent)
    if ua:
        fields.append({"name": FIELD_USER_AGENT, "value": ua, "inline": False})

    return fields


def build_payload(app_name: str, kind: str = KIND_LOGIN, **values: Any) -> dict:
    """ログイン / 新規ユーザー登録の通知ペイロードを共通フォーマットで組む。"""
    if kind == KIND_SIGNUP:
        title = f"🎉 {app_name} 新規ユーザー登録"
        color = COLOR_SIGNUP
    else:
        title = f"🔐 {app_name} ログイン"
        color = COLOR_LOGIN

    return {
        "title": title,
        "message": "",
        "level": "info",
        "color": color,
        "fields": build_fields(**values),
        # ログイン通知は用途別の1チャンネルへ集約しているため、送信元を必ず載せる
        "source": app_name,
    }


def field_value(fields: Optional[List[Dict[str, Any]]], name: str) -> Optional[str]:
    """フィールド一覧から名前で値を引く（見つからなければ None）。"""
    if not isinstance(fields, list):
        return None
    for field in fields:
        if isinstance(field, dict) and str(field.get("name") or "").strip() == name:
            value = str(field.get("value") or "").strip()
            if value:
                return value
    return None
