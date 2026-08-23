"""認証: Supabase Auth の JWT / セッション Cookie / API キー（/api/* 用）

優先順位は Bearer → Cookie。
- `Authorization: Bearer <Supabase の access_token>` … ブラウザ・PWA の通常経路
- `Authorization: Bearer sk_...`                     … スクリプトからの API キー
- セッション Cookie                                   … SSE 専用

**Cookie が残っているのは EventSource が Authorization ヘッダーを付けられないため。**
`POST /auth/session` が Supabase の JWT を検証したうえでのみ発行する短命 Cookie で、
Google OAuth のような独自ログイン経路はもう存在しない。
"""

import asyncio
import hashlib
import os
import secrets
from typing import Optional, Set

from fastapi import HTTPException, Request
from itsdangerous import BadData, URLSafeTimedSerializer

import supabase_auth

APP_URL = os.getenv("APP_URL", "/")
ALLOWED_EMAILS: Set[str] = {
    e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()
}
SECRET_KEY = os.getenv("SECRET_KEY", "")

SESSION_COOKIE = "signaly_session"
# 本番・開発トンネルはどちらも HTTPS。APP_URL から判定して Secure を付ける
# （ローカルの http://127.0.0.1 直アクセスでは付かない）
SESSION_COOKIE_SECURE = APP_URL.startswith("https://")
# Supabase のアクセストークンは 1 時間で切れ、フロントエンドは更新のたびに
# この Cookie を貼り直す。長く持たせる意味が無いので 1 日で失効させる。
SESSION_MAX_AGE = 60 * 60 * 24

API_KEY_PREFIX = "sk_"


def _signer() -> URLSafeTimedSerializer:
    key = SECRET_KEY or "dev-only-insecure-key"
    return URLSafeTimedSerializer(key, salt="signaly-auth")


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def api_key_prefix(key: str) -> str:
    return key[:12] if len(key) >= 12 else key


def sign_value(value: str) -> str:
    return _signer().dumps(value)


def load_signed_value(token: str, max_age: int) -> str:
    return _signer().loads(token, max_age=max_age)


def _get_session_email(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        email = load_signed_value(token, SESSION_MAX_AGE)
    except BadData:
        return None
    # Cookie 発行後に許可リストから外れた場合に備え、毎回引き直す
    return email if email in ALLOWED_EMAILS else None


def _get_bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def get_session_email(request: Request) -> Optional[str]:
    return _get_session_email(request)


def is_allowed_email(email: str) -> bool:
    return bool(email) and email.lower() in ALLOWED_EMAILS


async def verify_supabase_token(token: str) -> str:
    """Supabase の access_token を検証し、許可されたメールアドレスを返す。

    署名・有効期限・発行元の検証は supabase_auth 側で行う。
    許可ユーザーの判定はここで行い、通らなければ 403 にする
    （401 にすると、フロントエンドが「更新すれば通る」と誤解する）。
    """
    try:
        claims = await asyncio.to_thread(supabase_auth.verify_access_token, token)
    except supabase_auth.SupabaseAuthError as e:
        raise HTTPException(status_code=e.status, detail=e.message)

    email = supabase_auth.email_from_claims(claims)
    if not is_allowed_email(email):
        raise HTTPException(
            status_code=403,
            detail="このアカウントはアクセスが許可されていません",
        )
    return email


# main.py が起動時に差し替える
_resolve_api_key: Optional[callable] = None


def set_api_key_resolver(resolver) -> None:
    global _resolve_api_key
    _resolve_api_key = resolver


async def require_auth(request: Request) -> str:
    bearer = _get_bearer_token(request)
    if bearer:
        if bearer.startswith(API_KEY_PREFIX):
            resolved = _resolve_api_key(bearer) if _resolve_api_key else None
            if resolved:
                return resolved
        else:
            # 検証に失敗したらここで 401 / 403 を上げる。
            # Cookie へフォールバックすると、期限切れトークンを持つ端末が
            # いつまでも古い Cookie で通り続けてしまう。
            return await verify_supabase_token(bearer)

    email = _get_session_email(request)
    if email:
        return email

    raise HTTPException(status_code=401, detail="Unauthorized")
