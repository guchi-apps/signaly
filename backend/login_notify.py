"""Signaly へのログイン成功時の Webhook 通知

**Supabase の Database Webhooks（/notify/app-login/{app_id}）では代替できない。**
Supabase プロジェクトは複数アプリで共有しており、`auth.users` / `auth.sessions` は
プロジェクト全体で1つしかない。そこへ掛けた Database Webhook は他アプリのログインでも
発火し、`{app_id}` は設定時に選んだ表示名でしかないため、どのアプリへのログインかを
区別できない（#110 の計画レビューで判明）。

そのため Signaly 自身のログインは、フロントエンドが認証コールバックを終えた時点で
呼ぶ `POST /auth/session`（`event: "login"`）を起点に、ここから通知する。
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)

SIGNALY_LOGIN_WEBHOOK_URL = os.getenv("SIGNALY_LOGIN_WEBHOOK_URL", "").strip()
APP_NAME = "Signaly"  # 通知タイトルに使うアプリ名。他アプリへ流用する場合はここだけ変更する


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def user_info_from_claims(claims: Dict[str, Any]) -> Dict[str, Any]:
    """検証済みの Supabase JWT から、通知に出してよい項目だけを取り出す。

    クレームには `session_id` など通知に出す必要のない値も入るため、
    ここでホワイトリストして以降はこの辞書だけを扱う。
    """
    user_meta = claims.get("user_metadata")
    user_meta = user_meta if isinstance(user_meta, dict) else {}
    app_meta = claims.get("app_metadata")
    app_meta = app_meta if isinstance(app_meta, dict) else {}

    return {
        "name": user_meta.get("full_name") or user_meta.get("name"),
        "provider": app_meta.get("provider"),
        "verified_email": claims.get("email_verified"),
    }


def build_login_notification(
    email: str,
    user_info: Dict[str, Any],
    request: Request,
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ua = (request.headers.get("user-agent") or "unknown")[:500]

    fields = []
    name = user_info.get("name")
    if name:
        fields.append({"name": "ユーザー", "value": str(name), "inline": True})
    fields.append({"name": "メール", "value": email, "inline": True})

    provider = user_info.get("provider")
    if provider:
        fields.append({"name": "プロバイダ", "value": str(provider), "inline": True})

    fields.append({"name": "接続元IP", "value": client_ip(request), "inline": True})

    verified = user_info.get("verified_email")
    if verified is not None:
        fields.append({
            "name": "メール確認済",
            "value": "はい" if verified else "いいえ",
            "inline": True,
        })

    fields.append({"name": "日時", "value": now, "inline": False})
    fields.append({"name": "User-Agent", "value": ua, "inline": False})

    return {
        "title": f"🔐 {APP_NAME} ログイン",
        "message": "",
        "level": "info",
        "color": "#57f287",
        "fields": fields,
        # ログイン通知を他アプリと同じチャンネルへ集約しても見分けられるようにする
        "source": APP_NAME,
    }


async def send_login_notification(payload: dict) -> None:
    if not SIGNALY_LOGIN_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(SIGNALY_LOGIN_WEBHOOK_URL, json=payload)
            response.raise_for_status()
    except Exception:
        logger.exception("ログイン通知の送信に失敗しました")
