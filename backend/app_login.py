"""Supabase Database Webhooks から届くアプリのログイン通知を Signaly 形式へ変換する

アプリ側にログイン通知用のコードを持たせずに済ませるための経路。
Supabase Auth へ移行したアプリは OAuth コールバックを自分のバックエンドで処理しないため、
アプリのコードへフックしてログイン通知を送る方式が使えない。
代わりに Supabase の Database Webhooks（auth.users / auth.sessions の変更を HTTP POST する機能）
を Signaly へ向け、ここで通知へ変換する。

Supabase が送るペイロードは形式が固定で変更できないため、既存の /webhook/{channel_id}
（Discord 互換）では受けられない。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import login_format

# auth.users の行にはパスワードハッシュや各種トークンが含まれる。
# record を丸ごと通知へ出すと機密が Signaly の通知履歴に残るため、
# 通知へ載せるキーは必ずこのモジュール内のホワイトリスト経由でのみ取り出す。
TOKEN_HEADER = "x-signaly-token"

MAX_VALUE_LEN = login_format.MAX_VALUE_LEN
APP_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 通知しない理由（レスポンスの skipped に入る）
SKIP_DELETE = "delete"
SKIP_NO_SIGN_IN = "no_sign_in"


def valid_app_id(app_id: str) -> bool:
    return bool(APP_ID_PATTERN.match(app_id))


def extract_token(headers, query_token: Optional[str] = None) -> Optional[str]:
    """リクエストからチャンネルID（＝トークン）を取り出す。

    Supabase の Database Webhooks はカスタムヘッダーを付けられるが、
    UI の都合でどの形が使えるかが分からないため 3 経路を受け付ける。
    優先度: X-Signaly-Token → Authorization: Bearer → ?token=
    """
    token = (headers.get(TOKEN_HEADER) or "").strip()
    if token:
        return token

    authorization = (headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token

    token = (query_token or "").strip()
    return token or None


def _truncate(value: str) -> str:
    return value if len(value) <= MAX_VALUE_LEN else value[:MAX_VALUE_LEN]


def _text(value: Any) -> Optional[str]:
    """スカラー値だけを文字列化する。dict / list は通知に出さない。"""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    text = str(value).strip()
    return _truncate(text) if text else None


def _dict(record: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def _first_text(source: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for key in keys:
        text = _text(source.get(key))
        if text:
            return text
    return None


def build_fields(record: Dict[str, Any]) -> List[dict]:
    """Supabase の行データから通知フィールドを組む（ホワイトリスト方式）。

    名前・並び・日時の書式は `login_format` が持つ共通フォーマットに従う（#204）。
    auth.users には email / raw_*_meta_data が、auth.sessions には ip / user_agent が入る。
    どちらのテーブルが起点でも動くよう、存在する項目だけを拾う。
    """
    user_meta = _dict(record, "raw_user_meta_data")
    app_meta = _dict(record, "raw_app_meta_data")

    confirmed: Any = None
    if "email_confirmed_at" in record or "confirmed_at" in record:
        confirmed = bool(record.get("email_confirmed_at") or record.get("confirmed_at"))

    return login_format.build_fields(
        user=_first_text(user_meta, ["full_name", "name", "user_name"]),
        email=_text(record.get("email")) or _text(user_meta.get("email")),
        provider=_first_text(app_meta, ["provider"]),
        ip=_text(record.get("ip")),
        email_verified=confirmed,
        user_id=_first_text(record, ["user_id", "id"]),
        timestamp=_first_text(record, ["last_sign_in_at", "created_at"]),
        user_agent=_text(record.get("user_agent")),
    )


def _classify(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(イベント種別, 通知しない理由) を返す。

    種別は "login" / "signup" / "other" のいずれか。
    """
    event_type = str(payload.get("type") or "").upper()
    table = str(payload.get("table") or "")
    record = payload.get("record")
    old_record = payload.get("old_record")

    if event_type == "DELETE":
        return None, SKIP_DELETE
    if not isinstance(record, dict):
        # DELETE 以外で record が無い＝解釈できない。取りこぼしを黙って捨てない
        return "other", None

    if table == "sessions":
        return ("login" if event_type == "INSERT" else "other"), None

    if table == "users":
        if event_type == "INSERT":
            return "signup", None
        if event_type == "UPDATE":
            # auth.users の UPDATE はログイン以外（メール変更・メタデータ更新など）でも
            # 飛ぶため、last_sign_in_at が動いたときだけログインとみなす。
            old = old_record if isinstance(old_record, dict) else {}
            if record.get("last_sign_in_at") == old.get("last_sign_in_at"):
                return None, SKIP_NO_SIGN_IN
            return "login", None
        return "other", None

    return "other", None


def parse_app_login_payload(app_id: str, payload: Dict[str, Any]) -> Optional[dict]:
    """Supabase の生ペイロードを Signaly 内部形式へ変換する。

    通知しない場合は None を返す（呼び出し側は 200 を返す）。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    kind, _ = _classify(payload)
    if kind is None:
        return None

    record = payload.get("record")
    record = record if isinstance(record, dict) else {}
    fields = build_fields(record)

    if kind in (login_format.KIND_LOGIN, login_format.KIND_SIGNUP):
        notification = login_format.build_payload(app_id, kind)
        notification["fields"] = fields or None
        return notification

    schema = _text(payload.get("schema")) or "?"
    table = _text(payload.get("table")) or "?"
    event_type = _text(payload.get("type")) or "?"
    return {
        "title": f"🔔 {app_id} イベント",
        "message": f"{schema}.{table} / {event_type}",
        "level": "info",
        "color": None,
        "fields": fields or None,
        # URL パスの app_id はアプリを一意に表すので、そのまま送信元にする。
        # ログイン通知を1本のチャンネルへ統合しても、どのアプリのログインかを絞り込める。
        "source": app_id,
    }


def skip_reason(payload: Dict[str, Any]) -> Optional[str]:
    """通知しない場合の理由を返す（レスポンス用）。"""
    if not isinstance(payload, dict):
        return None
    return _classify(payload)[1]
