"""Google ログイン成功時の Signaly Webhook 通知"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)

LOGIN_WEBHOOK_URL = os.getenv("LOGIN_WEBHOOK_URL", "").strip()
APP_NAME = "Signaly"  # 通知タイトルに使うアプリ名。他アプリへ流用する場合はここだけ変更する


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


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
    if not LOGIN_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(LOGIN_WEBHOOK_URL, json=payload)
            response.raise_for_status()
    except Exception:
        logger.exception("ログイン通知の送信に失敗しました")
