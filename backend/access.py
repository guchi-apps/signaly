"""StatusHub の共通アクセス設定（判定API）のクライアント（#319）

ログイン許可の正本は StatusHub（guchi-apps/status-hub の docs/access-control.md）。
管理画面での追加・変更・取り消しが、再デプロイや 1Password の編集なしで反映される。

契約:
- 結果は `ttlSeconds` だけ使い回す
- 取得に失敗したときは、直前に取得できた判定を `maxStaleSeconds` まで使う。超えたら拒否する
- 一度も判定できていない利用者は拒否する（許可を広げない）
- **旧環境変数（ALLOWED_EMAILS）を判定にもフォールバックにも使わない。**
  判定APIが使えないときに旧リストで通すと、StatusHub で取り消した利用者が通ってしまう
- アプリ別トークンが取れないときも通信せず失敗として扱う（＝全員拒否。未設定が「誰でも通す」に化けない）

アプリ別トークンは issue-deck の共有トークン `SIGNALY_ACCESS_APP_TOKEN` から読む。管理画面の
「トークン発行」が自動で書き込むため、1Password・GitHub Secrets・デプロイを経由しない。
再発行で古いトークンは即失効するので、401 ならキャッシュを捨てて読み直し、1回だけ再試行する。
**トークンの値はログへ出さない。**

副作用（時計・通信）は引数で受け、`unittest` で単体に動かせるようにしている。
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("signaly.access")

TIMEOUT_SECONDS = 5
DEFAULT_ACCESS_API_URL = "https://admin.gucchii.com"
TOKEN_NAME = "SIGNALY_ACCESS_APP_TOKEN"
# 共有トークン API へ名乗る利用元
CONSUMER = "signaly"
# 共有トークンのキャッシュ寿命（再発行の反映は最大これだけ遅れる）
SHARED_TOKEN_CACHE_SECONDS = 10 * 60
# ハートビートは 5 分以内ごとに 1 回（余裕を見て 4 分）
HEARTBEAT_INTERVAL_SECONDS = 4 * 60


class AccessUnavailable(Exception):
    """判定APIから有効な応答を得られなかった（通信失敗・5xx・不正な応答）。"""


@dataclass(frozen=True)
class Subject:
    sub: str
    email: str
    email_verified: bool

    def payload(self) -> dict:
        return {"sub": self.sub, "email": self.email, "emailVerified": self.email_verified}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    permissions: Tuple[str, ...] = ()
    reason: Optional[str] = None


@dataclass(frozen=True)
class Response:
    app_version: int
    ttl_seconds: float
    max_stale_seconds: float
    decision: Optional[Decision] = None


DENY = Decision(allowed=False, reason="unavailable")


def parse_response(payload: object, expect_decision: bool) -> Response:
    """応答の形を確かめる。形が違えば AccessUnavailable を投げ、失敗として扱わせる。"""
    if not isinstance(payload, dict):
        raise AccessUnavailable("unexpected payload")

    def positive(v: object) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0

    if not (
        positive(payload.get("appVersion"))
        and positive(payload.get("ttlSeconds"))
        and positive(payload.get("maxStaleSeconds"))
    ):
        raise AccessUnavailable("unexpected payload")

    decision = None
    if expect_decision:
        d = payload.get("decision")
        if not isinstance(d, dict) or not isinstance(d.get("allowed"), bool):
            raise AccessUnavailable("unexpected payload")
        perms = d.get("permissions")
        permissions = tuple(p for p in perms if isinstance(p, str)) if isinstance(perms, list) else ()
        reason = d.get("reason")
        decision = Decision(
            allowed=d["allowed"],
            permissions=permissions if d["allowed"] else (),
            reason=reason if isinstance(reason, str) else None,
        )
    return Response(
        app_version=int(payload["appVersion"]),
        ttl_seconds=float(payload["ttlSeconds"]),
        max_stale_seconds=float(payload["maxStaleSeconds"]),
        decision=decision,
    )


# fetcher(body) -> Response。通信まわりは差し替えられる
Fetcher = Callable[[dict], Response]


@dataclass
class _CacheEntry:
    decision: Decision
    fetched_at: float
    ttl: float
    max_stale: float


@dataclass
class AccessClient:
    fetcher: Fetcher
    now: Callable[[], float] = time.monotonic
    applied_version: Optional[int] = None
    _cache: Dict[Tuple[str, str], _CacheEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def decide(self, subject: Subject) -> Decision:
        # 検証済みでない ID は問い合わせず拒否する（契約でも unverified_identity）
        if not subject.sub or not subject.email or not subject.email_verified:
            return Decision(allowed=False, reason="unverified_identity")

        key = (subject.sub, subject.email.lower())
        with self._lock:
            cached = self._cache.get(key)
            applied = self.applied_version
        if cached and self.now() - cached.fetched_at < cached.ttl:
            return cached.decision

        body: dict = {"subject": subject.payload()}
        if applied is not None:
            body["appliedVersion"] = applied
        try:
            response = self.fetcher(body)
            if response.decision is None:
                raise AccessUnavailable("missing decision")
        except Exception as e:  # 通信失敗・不正な応答はすべて「取得失敗」
            logger.error("アクセス判定の取得に失敗: %s", _describe(e))
            # 直前の判定を、取得できた時刻から max_stale まで。超えたら（または無ければ）拒否
            if cached and self.now() - cached.fetched_at <= cached.max_stale:
                return cached.decision
            return DENY

        at = self.now()
        with self._lock:
            self.applied_version = response.app_version
            self._cache[key] = _CacheEntry(
                decision=response.decision,
                fetched_at=at,
                ttl=response.ttl_seconds,
                max_stale=response.max_stale_seconds,
            )
        return response.decision

    def heartbeat(self) -> bool:
        """判定なしの確認。反映状況（appliedVersion）を StatusHub へ伝える。"""
        with self._lock:
            applied = self.applied_version
        body: dict = {}
        if applied is not None:
            body["appliedVersion"] = applied
        try:
            response = self.fetcher(body)
        except Exception as e:
            logger.error("アクセス設定のハートビートに失敗: %s", _describe(e))
            return False
        with self._lock:
            self.applied_version = response.app_version
        return True


def _describe(error: Exception) -> str:
    # 例外の文字列にトークンやメールが混ざらないよう、型と HTTP ステータスだけを出す
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, AccessUnavailable):
        return str(error)
    return type(error).__name__


# ---- 共有トークン（issue-deck） ----

_token_lock = threading.Lock()
_token_cache: Optional[Tuple[str, float]] = None  # (value, fetched_at)


def forget_shared_token() -> None:
    global _token_cache
    with _token_lock:
        _token_cache = None


def get_shared_token(now: Callable[[], float] = time.monotonic) -> Optional[str]:
    """アクセス判定用トークンを返す。取れなければ（古くても）直前の値、それも無ければ None。

    ローカル開発では共有トークン API を使わず、環境変数 `ACCESS_APP_TOKEN` を直接指定できる。
    """
    global _token_cache
    with _token_lock:
        cached = _token_cache
        if cached and now() - cached[1] < SHARED_TOKEN_CACHE_SECONDS:
            return cached[0]

        base = os.getenv("ISSUE_DECK_URL", "").strip().rstrip("/")
        secret = os.getenv("SHARED_TOKEN_API_SECRET", "").strip()
        if not base or not secret:
            return (cached[0] if cached else None) or os.getenv("ACCESS_APP_TOKEN", "").strip() or None

        try:
            req = urllib.request.Request(
                f"{base}/api/shared-tokens?name={urllib.parse.quote(TOKEN_NAME)}",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "X-Shared-Token-Consumer": CONSUMER,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
                payload = json.loads(res.read())
            value = payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(value, str) or not value:
                raise AccessUnavailable("unexpected payload")
        except Exception as e:
            logger.error("共有トークンの取得に失敗: %s", _describe(e))
            return cached[0] if cached else None

        _token_cache = (value, now())
        return value


def _post(token: str, body: dict) -> dict:
    base = (os.getenv("ACCESS_API_URL", "").strip() or DEFAULT_ACCESS_API_URL).rstrip("/")
    req = urllib.request.Request(
        f"{base}/api/access/v1/decision",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
        return json.loads(res.read())


def http_fetcher(body: dict) -> Response:
    token = get_shared_token()
    if not token:
        raise AccessUnavailable(f"{TOKEN_NAME} が未設定")
    try:
        payload = _post(token, body)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        # 再発行で古いトークンが失効している。読み直して 1 回だけ再試行する
        forget_shared_token()
        renewed = get_shared_token()
        if not renewed or renewed == token:
            raise
        payload = _post(renewed, body)
    return parse_response(payload, expect_decision="subject" in body)


_client: Optional[AccessClient] = None
_client_lock = threading.Lock()


def client() -> AccessClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AccessClient(fetcher=http_fetcher)
        return _client


def is_allowed(subject: Subject) -> bool:
    return client().decide(subject).allowed


def send_heartbeat() -> bool:
    return client().heartbeat()
