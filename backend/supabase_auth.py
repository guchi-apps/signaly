"""Supabase Auth が発行したアクセストークン（JWT）の検証

Signaly のログインは Supabase Auth の Google ログインへ移行した。
フロントエンドは `Authorization: Bearer <access_token>` を付けて /api/* を叩き、
ここでその JWT を検証する。

**デコードだけで済ませないこと。** ペイロードは誰でも作れるため、
必ず Supabase の JWKS（公開鍵）で署名を検証し、exp / iss / aud まで見る。

Supabase は非対称鍵（ES256 / RS256）で署名し、公開鍵を
`<SUPABASE_URL>/auth/v1/.well-known/jwks.json` で配る。旧方式の対称鍵（HS256）は
JWT シークレットの配布が必要になるため受け付けない（JWKS に鍵が無いので自然に落ちる）。
"""

import os
from typing import Optional

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()

# Supabase が発行する JWT の aud は常にこの値
AUDIENCE = "authenticated"
ALGORITHMS = ["ES256", "RS256"]

# JWKS のキャッシュ寿命。鍵のローテーションに追随できる程度に短く保つ
JWKS_CACHE_TTL = 60 * 60

_jwks_client: Optional[PyJWKClient] = None


class SupabaseAuthError(Exception):
    """JWT の検証に失敗した。status は返すべき HTTP ステータス。"""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY)


def issuer() -> str:
    return f"{SUPABASE_URL}/auth/v1"


def jwks_url() -> str:
    return f"{issuer()}/.well-known/jwks.json"


def _client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            jwks_url(),
            cache_keys=True,
            lifespan=JWKS_CACHE_TTL,
        )
    return _jwks_client


def reset_jwks_cache() -> None:
    """テストと、SUPABASE_URL を差し替えたときのため。"""
    global _jwks_client
    _jwks_client = None


def verify_access_token(token: str) -> dict:
    """検証済みのクレームを返す。失敗時は SupabaseAuthError を送出する。

    ネットワーク（JWKS の取得）を伴うため、非同期の経路からは
    asyncio.to_thread 経由で呼ぶこと。
    """
    if not configured():
        raise SupabaseAuthError("Supabase Auth が設定されていません", status=503)

    try:
        signing_key = _client().get_signing_key_from_jwt(token)
    except PyJWKClientError:
        # 未知の kid・JWKS の取得失敗。どちらも「このトークンは通せない」で同じ扱い
        raise SupabaseAuthError("署名鍵を取得できませんでした")
    except InvalidTokenError:
        raise SupabaseAuthError("トークンの形式が不正です")

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALGORITHMS,
            audience=AUDIENCE,
            issuer=issuer(),
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except InvalidTokenError:
        # 例外メッセージにトークンの中身が混ざらないよう、理由は返さない
        raise SupabaseAuthError("トークンが無効です")

    return claims


def email_from_claims(claims: dict) -> str:
    email = claims.get("email")
    if not email:
        # メールが無いプロバイダ（電話番号ログイン等）は許可メール判定ができない
        return ""
    return str(email).strip().lower()


def user_id_from_claims(claims: dict) -> str:
    """Supabase のユーザー ID（他アプリと共通の UUID）。"""
    return str(claims.get("sub") or "")
