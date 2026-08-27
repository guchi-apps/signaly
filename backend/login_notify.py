"""Signaly へのログイン成功時の Webhook 通知

**Supabase の Database Webhooks では代替できない。** Supabase プロジェクトは複数アプリで
共有しており、`auth.users` / `auth.sessions` はプロジェクト全体で1つしかない。そこへ掛けた
Database Webhook は他アプリのログインでも発火し、行データにはどのアプリへのログインかを
示す情報が無い（#110 の計画レビューで判明）。受け口だった `/notify/app-login/{app_id}` は、
実際に他アプリのログインを別アプリ名で通知していたため #209 で削除した。

そのため Signaly 自身のログインは、フロントエンドが認証コールバックを終えた時点で
呼ぶ `POST /auth/session`（`event: "login"`）を起点に、ここから通知する。
**これが全アプリ共通の形**で、他アプリも自分の認証コールバックから同じように送る。

**通知の形は `login_format` に寄せている（#204）。** ログイン通知は全アプリで1本の
チャンネルへ集約しているため、ここだけ独自の形にすると並べたときに揃わない。
共通フォーマットの正は `docs/webhook.md`。
"""

import logging
import os
from typing import Any, Dict

import httpx
from fastapi import Request

import login_format

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
    return login_format.build_payload(
        APP_NAME,
        user=user_info.get("name"),
        email=email,
        provider=user_info.get("provider"),
        ip=client_ip(request),
        email_verified=user_info.get("verified_email"),
        user_agent=request.headers.get("user-agent") or "unknown",
    )


async def send_login_notification(payload: dict) -> None:
    if not SIGNALY_LOGIN_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(SIGNALY_LOGIN_WEBHOOK_URL, json=payload)
            response.raise_for_status()
    except Exception:
        logger.exception("ログイン通知の送信に失敗しました")
