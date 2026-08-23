import json
import asyncio
import hashlib
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, or_

import app_login
import auth
import supabase_auth
from database import ApiKey, Channel, ChannelGroup, Notification, PushSubscription, get_session, init_db
from notification_prefs import (
    delete_settings_for_target,
    get_notification_settings,
    set_channel_notification_setting,
    set_group_notification_setting,
)
from push import (
    get_application_server_key,
    push_configured,
    push_vapid_healthy,
    send_push_notifications,
    send_test_push_to_user,
    validate_push_config,
)
from webhook import parse_webhook_payload

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
DOCS_DIR = BASE_DIR.parent / "docs"
APP_VERSION = json.loads((BASE_DIR.parent / "version.json").read_text())["version"]

# channel_name → list of subscriber queues
_subscribers: Dict[str, List[asyncio.Queue]] = {}

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")


# ── DB helpers（threadpool で呼ぶ）────────────────────────────────────────────

def _fetch_channels() -> Dict[str, str]:
    """channel_id -> channel_name"""
    with get_session() as session:
        rows = session.query(Channel).all()
        return {row.id: row.name for row in rows}


def _webhook_url(request: Request, channel_id: str) -> str:
    base = auth.APP_URL
    if base.startswith(("http://", "https://")):
        return f"{base.rstrip('/')}/webhook/{channel_id}"
    return str(f"{request.base_url}webhook/{channel_id}")


def _next_channel_sort_order(session, group_id: Optional[str]) -> int:
    if group_id:
        filt = Channel.group_id == group_id
    else:
        filt = Channel.group_id.is_(None)
    max_order = (
        session.query(Channel.sort_order)
        .filter(filt)
        .order_by(Channel.sort_order.desc())
        .first()
    )
    return (max_order[0] + 1) if max_order else 0


def _create_channel(name: str, group_id: Optional[str] = None) -> Dict[str, str]:
    channel_id = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        if session.query(Channel).filter(Channel.name == name).first():
            raise ValueError("duplicate")
        if group_id and not session.query(ChannelGroup).filter(ChannelGroup.id == group_id).first():
            raise ValueError("group_not_found")
        sort_order = _next_channel_sort_order(session, group_id)
        session.add(
            Channel(
                id=channel_id,
                name=name,
                group_id=group_id,
                sort_order=sort_order,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return {"id": channel_id, "name": name, "group_id": group_id}


def _update_channel(
    channel_id: str,
    name: Optional[str] = None,
    group_id: Optional[str] = None,
    update_group_id: bool = False,
) -> Optional[Dict[str, str]]:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        row = session.query(Channel).filter(Channel.id == channel_id).first()
        if not row:
            return None
        old_name = row.name
        if name is not None:
            if name != old_name and session.query(Channel).filter(Channel.name == name).first():
                raise ValueError("duplicate")
            row.name = name
            row.updated_at = now
            if old_name != name:
                session.query(Notification).filter(Notification.channel == old_name).update(
                    {Notification.channel: name},
                    synchronize_session=False,
                )
        if update_group_id:
            if group_id is not None and not session.query(ChannelGroup).filter(
                ChannelGroup.id == group_id
            ).first():
                raise ValueError("group_not_found")
            row.group_id = group_id
            row.updated_at = now
        session.commit()
        result_name = row.name
        result_group_id = row.group_id
    if name is not None and old_name != name and old_name in _subscribers:
        _subscribers[result_name] = _subscribers.pop(old_name)
    return {"id": channel_id, "name": result_name, "group_id": result_group_id}


def _delete_channel(channel_id: str) -> Optional[str]:
    with get_session() as session:
        row = session.query(Channel).filter(Channel.id == channel_id).first()
        if not row:
            return None
        name = row.name
        session.query(Notification).filter(Notification.channel == name).delete(
            synchronize_session=False,
        )
        session.delete(row)
        session.commit()
    delete_settings_for_target("channel", channel_id)
    _subscribers.pop(name, None)
    return name


def _create_group(name: str) -> Dict[str, object]:
    group_id = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        if session.query(ChannelGroup).filter(ChannelGroup.name == name).first():
            raise ValueError("duplicate")
        max_order = session.query(ChannelGroup.sort_order).order_by(
            ChannelGroup.sort_order.desc()
        ).first()
        sort_order = (max_order[0] + 1) if max_order else 0
        session.add(
            ChannelGroup(
                id=group_id,
                name=name,
                sort_order=sort_order,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return {"id": group_id, "name": name, "sort_order": sort_order}


def _update_group(group_id: str, name: str) -> Optional[Dict[str, object]]:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        row = session.query(ChannelGroup).filter(ChannelGroup.id == group_id).first()
        if not row:
            return None
        if name != row.name and session.query(ChannelGroup).filter(ChannelGroup.name == name).first():
            raise ValueError("duplicate")
        row.name = name
        row.updated_at = now
        session.commit()
        return {"id": group_id, "name": name, "sort_order": row.sort_order}


def _delete_group(group_id: str) -> Optional[str]:
    with get_session() as session:
        row = session.query(ChannelGroup).filter(ChannelGroup.id == group_id).first()
        if not row:
            return None
        name = row.name
        session.delete(row)
        session.commit()
    delete_settings_for_target("group", group_id)
    return name


def _reorder_layout(groups: List[Dict[str, object]], channels: List[Dict[str, object]]) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        db_groups = {g.id: g for g in session.query(ChannelGroup).all()}
        db_channels = {c.id: c for c in session.query(Channel).all()}

        if {g["id"] for g in groups} != set(db_groups):
            raise ValueError("invalid_groups")
        if {c["id"] for c in channels} != set(db_channels):
            raise ValueError("invalid_channels")

        group_ids = set(db_groups)
        for item in groups:
            row = db_groups[item["id"]]
            row.sort_order = item["sort_order"]
            row.updated_at = now

        for item in channels:
            row = db_channels[item["id"]]
            group_id = item.get("group_id")
            if group_id is not None and group_id not in group_ids:
                raise ValueError("group_not_found")
            row.group_id = group_id
            row.sort_order = item["sort_order"]
            row.updated_at = now

        session.commit()


def _fetch_channels_tree(request: Request) -> Dict[str, object]:
    with get_session() as session:
        group_rows = session.query(ChannelGroup).order_by(
            ChannelGroup.sort_order, ChannelGroup.name
        ).all()
        channel_rows = session.query(Channel).all()

    channel_rows.sort(key=lambda r: (r.sort_order, r.name.lower()))

    groups_by_id = {
        g.id: {
            "id": g.id,
            "name": g.name,
            "sort_order": g.sort_order,
            "channels": [],
        }
        for g in group_rows
    }
    ungrouped: List[dict] = []
    flat: List[dict] = []

    for row in channel_rows:
        item = _channel_item(request, row.id, row.name, row.group_id)
        flat.append(item)
        if row.group_id and row.group_id in groups_by_id:
            groups_by_id[row.group_id]["channels"].append(item)
        else:
            ungrouped.append(item)

    return {
        "groups": list(groups_by_id.values()),
        "ungrouped": ungrouped,
        "channels": flat,
    }


def _resolve_api_key_email(key: str) -> Optional[str]:
    key_hash = auth.hash_secret(key)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        row = session.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()
        if not row:
            return None
        row.last_used_at = now
        session.commit()
        return row.email


def _create_api_key(email: str, name: str) -> dict:
    key = auth.generate_api_key()
    now = datetime.now(timezone.utc)
    key_id = str(uuid.uuid4())
    with get_session() as session:
        session.add(
            ApiKey(
                id=key_id,
                email=email,
                name=name,
                key_hash=auth.hash_secret(key),
                key_prefix=auth.api_key_prefix(key),
                created_at=now,
            )
        )
        session.commit()
    return {
        "id": key_id,
        "name": name,
        "key": key,
        "key_prefix": auth.api_key_prefix(key),
        "created_at": now.isoformat(),
    }


def _list_api_keys(email: str) -> List[dict]:
    with get_session() as session:
        rows = (
            session.query(ApiKey)
            .filter(ApiKey.email == email)
            .order_by(ApiKey.created_at.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "name": r.name,
                "key_prefix": r.key_prefix,
                "created_at": r.created_at.isoformat(),
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            }
            for r in rows
        ]


def _delete_api_key(email: str, key_id: str) -> bool:
    with get_session() as session:
        row = session.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.email == email).first()
        if not row:
            return False
        session.delete(row)
        session.commit()
        return True


def _utc_iso(dt: datetime) -> str:
    """Serialize as UTC ISO-8601 with Z suffix (MySQL round-trip may drop tzinfo)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"


def _broadcast(channel_name: str, event: str, data: dict) -> None:
    for q in list(_subscribers.get(channel_name, [])):
        try:
            q.put_nowait({"event": event, "data": data})
        except asyncio.QueueFull:
            _subscribers[channel_name].remove(q)


async def _dispatch_notification(channel_name: str, parsed: dict) -> dict:
    entry = {
        "id": str(uuid.uuid4()),
        "channel": channel_name,
        "title": parsed["title"],
        "message": parsed["message"],
        "level": parsed["level"],
        "color": parsed["color"],
        "fields": parsed["fields"],
        "timestamp": _utc_iso(datetime.now(timezone.utc)),
    }

    await asyncio.to_thread(_save_notification, entry)
    _broadcast(channel_name, "notification", entry)

    asyncio.create_task(asyncio.to_thread(send_push_notifications, entry))
    return entry


def _delete_notifications(ids: List[str]) -> Dict[str, List[str]]:
    """複数の通知を削除し、チャンネルごとに削除できたIDのリストを返す。"""
    deleted_by_channel: Dict[str, List[str]] = {}
    with get_session() as session:
        rows = session.query(Notification).filter(Notification.id.in_(ids)).all()
        for row in rows:
            deleted_by_channel.setdefault(row.channel, []).append(row.id)
            session.delete(row)
        session.commit()
    return deleted_by_channel


def _save_notification(entry: dict) -> None:
    with get_session() as session:
        session.add(
            Notification(
                id=entry["id"],
                channel=entry["channel"],
                title=entry["title"],
                message=entry["message"],
                level=entry["level"],
                timestamp=datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")),
                fields=json.dumps(entry["fields"], ensure_ascii=False) if entry.get("fields") else None,
                color=entry.get("color"),
            )
        )
        session.commit()


def _notification_to_dict(r: Notification) -> dict:
    return {
        "id": r.id,
        "channel": r.channel,
        "title": r.title,
        "message": r.message,
        "level": r.level,
        "timestamp": _utc_iso(r.timestamp),
        "fields": json.loads(r.fields) if getattr(r, "fields", None) else None,
        "color": getattr(r, "color", None),
    }


def _fetch_history(
    channel_name: str,
    limit: int,
    before_timestamp: Optional[datetime] = None,
    before_id: Optional[str] = None,
) -> tuple[List[dict], bool]:
    with get_session() as session:
        q = session.query(Notification).filter(Notification.channel == channel_name)
        if before_timestamp is not None:
            if before_id is not None:
                q = q.filter(
                    or_(
                        Notification.timestamp < before_timestamp,
                        and_(
                            Notification.timestamp == before_timestamp,
                            Notification.id < before_id,
                        ),
                    )
                )
            else:
                q = q.filter(Notification.timestamp < before_timestamp)
        rows = (
            q.order_by(Notification.timestamp.desc(), Notification.id.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        return [_notification_to_dict(r) for r in rows[:limit]], has_more


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_notifications(query: str, limit: int, channel_name: Optional[str] = None) -> List[dict]:
    pattern = f"%{_escape_like(query)}%"
    with get_session() as session:
        q = session.query(Notification).filter(
            or_(
                Notification.title.like(pattern, escape="\\"),
                Notification.message.like(pattern, escape="\\"),
            )
        )
        if channel_name:
            q = q.filter(Notification.channel == channel_name)
        rows = q.order_by(Notification.timestamp.desc()).limit(limit).all()
        return [_notification_to_dict(r) for r in rows]


def _endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def _upsert_push_subscription(email: str, endpoint: str, p256dh: str, auth: str) -> None:
    now = datetime.now(timezone.utc)
    ep_hash = _endpoint_hash(endpoint)
    with get_session() as session:
        existing = session.query(PushSubscription).filter(PushSubscription.endpoint_hash == ep_hash).first()
        if existing:
            existing.email = email
            existing.endpoint = endpoint
            existing.p256dh = p256dh
            existing.auth = auth
            existing.updated_at = now
        else:
            session.add(
                PushSubscription(
                    id=str(uuid.uuid4()),
                    email=email,
                    endpoint_hash=ep_hash,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()


def _delete_push_subscription(endpoint: str) -> None:
    ep_hash = _endpoint_hash(endpoint)
    with get_session() as session:
        row = session.query(PushSubscription).filter(PushSubscription.endpoint_hash == ep_hash).first()
        if row:
            session.delete(row)
            session.commit()


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth.set_api_key_resolver(_resolve_api_key_email)
    if push_configured():
        try:
            await asyncio.to_thread(validate_push_config)
            logging.info("Web Push (VAPID) configured OK")
        except Exception:
            logging.exception("Web Push (VAPID) key load failed — push notifications disabled")
    yield


app = FastAPI(title="Signaly", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_frontend_assets(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    is_current_version = request.query_params.get("v") == APP_VERSION
    if path.endswith((".js", ".css")) and is_current_version and response.status_code < 400:
        # ?v=<現在のバージョン> の静的ファイルは、内容が変わればバージョンも
        # 上がる（bump_version.py が ?v= を同期する）ため長期キャッシュしてよい。
        # ここを no-cache にしていると起動のたびに全アセットの再検証待ちが発生し、
        # PWA の起動が遅くなる。
        # クエリの値を検証しないと、?v= の更新忘れや無関係なクエリでも
        # immutable 扱いになってしまうため、現在のバージョンと一致する場合のみ許可する。
        # エラーレスポンスまで immutable キャッシュすると、障害が直っても
        # ブラウザ・CDN 側に壊れたレスポンスが1年間残り続けてしまう。
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ── Auth endpoints ────────────────────────────────────────────────────────────
#
# ログインは Supabase Auth（Google）が行い、バックエンドはトークンを検証するだけ。
# フロントエンドは /api/auth/config で接続先を受け取り、supabase-js で認可 URL へ飛ばす。


@app.get("/api/auth/config")
async def auth_config():
    """フロントエンドが Supabase へ接続するための公開値。

    publishable key はブラウザへ配る前提の値なので認証を掛けない。
    service_role キーはここにも置かない。
    """
    if not supabase_auth.configured():
        raise HTTPException(status_code=503, detail="Supabase Auth が設定されていません")
    return {
        "supabaseUrl": supabase_auth.SUPABASE_URL,
        "supabasePublishableKey": supabase_auth.SUPABASE_PUBLISHABLE_KEY,
    }


@app.get("/auth/callback")
async def auth_callback_page():
    """Supabase からのリダイレクト先。

    実際のトークン受け取りはブラウザ側（frontend/auth/callback.html）で行う。
    StaticFiles のディレクトリ表示に頼らず、拡張子なしの固定 URL を
    Supabase の Redirect URLs へ登録できるようここで配る。
    """
    return FileResponse(FRONTEND_DIR / "auth" / "callback.html", media_type="text/html")


class SessionRequest(BaseModel):
    access_token: str


@app.post("/auth/session")
async def auth_session(payload: SessionRequest, response: Response):
    """検証済みの Supabase トークンと引き換えに、SSE 用のセッション Cookie を発行する。

    EventSource は Authorization ヘッダーを付けられないため、ここだけ Cookie に頼る。
    トークンを URL へ載せないための経路であって、独自のログイン手段ではない。
    """
    email = await auth.verify_supabase_token(payload.access_token)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.sign_value(email),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=auth.SESSION_COOKIE_SECURE,
    )
    return {"email": email}


@app.get("/auth/me")
async def auth_me(email: str = Depends(auth.require_auth)):
    return {"email": email}


@app.post("/auth/logout")
async def auth_logout(response: Response):
    # Supabase 側のセッション破棄はフロントエンド（supabase.auth.signOut）が行う
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


# ── Webhook（認証不要：外部サービスから叩く）──────────────────────────────────
# Discord Execute Webhook と同じ JSON 形式（content / embeds 等）を受け付ける。
# Signaly レガシー形式（message / title / level 等）も引き続き利用可能。


def _channel_item(
    request: Request,
    channel_id: str,
    name: str,
    group_id: Optional[str] = None,
) -> dict:
    item = {
        "id": channel_id,
        "name": name,
        "webhook_url": _webhook_url(request, channel_id),
    }
    if group_id:
        item["group_id"] = group_id
    return item


async def _read_webhook_body(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        raw = form.get("payload_json")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="payload_json が不正な JSON です") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="payload_json は JSON オブジェクトである必要があります")
        return data

    try:
        body = await request.body()
    except Exception:
        body = b""
    if not body.strip():
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="リクエストボディが不正な JSON です") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="リクエストボディは JSON オブジェクトである必要があります")
    return data


@app.post("/webhook/{channel_id}")
async def receive_webhook(channel_id: str, request: Request):
    channels = await asyncio.to_thread(_fetch_channels)
    if channel_id not in channels:
        raise HTTPException(status_code=404, detail="Channel not found")

    raw = await _read_webhook_body(request)
    try:
        parsed = parse_webhook_payload(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    channel_name = channels[channel_id]
    entry = await _dispatch_notification(channel_name, parsed)
    return {"ok": True, "id": entry["id"]}


# ── アプリのログイン通知（Supabase Database Webhooks 用）──────────────────────
# Supabase Auth へ移行したアプリは OAuth コールバックを自分のバックエンドで処理しないため、
# アプリ側のコードにログイン通知を差し込めない。Supabase 側の Database Webhooks を
# ここへ向けることで、アプリのコードを一切変更せずに通知を集約する。
#
# 宛先と認証はどちらもチャンネルID（token_urlsafe(16)）で行う。/webhook/{channel_id} が
# 既に「チャンネルIDが宛先の識別子であり事実上の資格情報」というモデルなので、
# 新しいシークレットを増やさずそれに揃える。URL パスの {app_id} は表示名にのみ使う。

@app.post("/notify/app-login/{app_id}")
async def receive_app_login(app_id: str, request: Request, token: Optional[str] = None):
    if not app_login.valid_app_id(app_id):
        raise HTTPException(status_code=400, detail="app_id が不正です")

    presented = app_login.extract_token(request.headers, token)
    if not presented:
        raise HTTPException(status_code=401, detail="Unauthorized")

    channels = await asyncio.to_thread(_fetch_channels)
    channel_name = channels.get(presented)
    if not channel_name:
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw = await _read_webhook_body(request)
    try:
        parsed = app_login.parse_app_login_payload(app_id, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if parsed is None:
        # ログインではないイベント（DELETE・last_sign_in_at が動かない UPDATE）。
        # Supabase 側のリトライ・エラーログを増やさないよう 200 で返す。
        return {"ok": True, "skipped": app_login.skip_reason(raw)}

    entry = await _dispatch_notification(channel_name, parsed)
    return {"ok": True, "id": entry["id"]}


# ── API（要認証）─────────────────────────────────────────────────────────────

class CreateChannelRequest(BaseModel):
    name: str
    group_id: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("チャンネル名を入力してください")
        if len(v) > 100:
            raise ValueError("チャンネル名は100文字以内にしてください")
        return v

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class UpdateChannelRequest(BaseModel):
    name: Optional[str] = None
    group_id: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("チャンネル名を入力してください")
        if len(v) > 100:
            raise ValueError("チャンネル名は100文字以内にしてください")
        return v

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class CreateGroupRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("グループ名を入力してください")
        if len(v) > 100:
            raise ValueError("グループ名は100文字以内にしてください")
        return v


class UpdateGroupRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("グループ名を入力してください")
        if len(v) > 100:
            raise ValueError("グループ名は100文字以内にしてください")
        return v


class LayoutGroupItem(BaseModel):
    id: str
    sort_order: int


class LayoutChannelItem(BaseModel):
    id: str
    group_id: Optional[str] = None
    sort_order: int


class ReorderLayoutRequest(BaseModel):
    groups: List[LayoutGroupItem]
    channels: List[LayoutChannelItem]


class ChannelNotificationSettingRequest(BaseModel):
    enabled: Optional[bool] = None


class GroupNotificationSettingRequest(BaseModel):
    enabled: bool


class DeleteNotificationsRequest(BaseModel):
    ids: List[str]

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("削除する通知を選択してください")
        return v


@app.get("/api/channels")
async def get_channels(request: Request, email: str = Depends(auth.require_auth)):
    return await asyncio.to_thread(_fetch_channels_tree, request)


@app.put("/api/channels/layout")
async def reorder_channels_layout(
    body: ReorderLayoutRequest,
    email: str = Depends(auth.require_auth),
):
    try:
        await asyncio.to_thread(
            _reorder_layout,
            [g.model_dump() for g in body.groups],
            [c.model_dump() for c in body.channels],
        )
    except ValueError as exc:
        if str(exc) == "group_not_found":
            raise HTTPException(status_code=404, detail="グループが見つかりません")
        raise HTTPException(status_code=400, detail="並び替えデータが不正です")
    return {"ok": True}


@app.post("/api/channels")
async def create_channel(
    request: Request,
    body: CreateChannelRequest,
    email: str = Depends(auth.require_auth),
):
    try:
        created = await asyncio.to_thread(_create_channel, body.name, body.group_id)
    except ValueError as exc:
        if str(exc) == "group_not_found":
            raise HTTPException(status_code=404, detail="グループが見つかりません")
        raise HTTPException(status_code=409, detail="同じ名前のチャンネルが既に存在します")

    return _channel_item(request, created["id"], created["name"], created.get("group_id"))


@app.patch("/api/channels/{channel_id}")
async def update_channel(
    channel_id: str,
    request: Request,
    body: UpdateChannelRequest,
    email: str = Depends(auth.require_auth),
):
    if body.name is None and body.group_id is None:
        raise HTTPException(status_code=400, detail="更新する項目を指定してください")
    try:
        updated = await asyncio.to_thread(
            _update_channel,
            channel_id,
            body.name,
            body.group_id,
            "group_id" in body.model_fields_set,
        )
    except ValueError as exc:
        if str(exc) == "group_not_found":
            raise HTTPException(status_code=404, detail="グループが見つかりません")
        raise HTTPException(status_code=409, detail="同じ名前のチャンネルが既に存在します")
    if not updated:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _channel_item(
        request,
        updated["id"],
        updated["name"],
        updated.get("group_id"),
    )


@app.delete("/api/channels/{channel_id}")
async def delete_channel(channel_id: str, email: str = Depends(auth.require_auth)):
    name = await asyncio.to_thread(_delete_channel, channel_id)
    if not name:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"ok": True, "name": name}


@app.get("/api/groups")
async def get_groups(request: Request, email: str = Depends(auth.require_auth)):
    tree = await asyncio.to_thread(_fetch_channels_tree, request)
    return {"groups": tree["groups"]}


@app.post("/api/groups")
async def create_group(body: CreateGroupRequest, email: str = Depends(auth.require_auth)):
    try:
        created = await asyncio.to_thread(_create_group, body.name)
    except ValueError:
        raise HTTPException(status_code=409, detail="同じ名前のグループが既に存在します")
    return created


@app.patch("/api/groups/{group_id}")
async def update_group(
    group_id: str,
    body: UpdateGroupRequest,
    email: str = Depends(auth.require_auth),
):
    try:
        updated = await asyncio.to_thread(_update_group, group_id, body.name)
    except ValueError:
        raise HTTPException(status_code=409, detail="同じ名前のグループが既に存在します")
    if not updated:
        raise HTTPException(status_code=404, detail="Group not found")
    return updated


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, email: str = Depends(auth.require_auth)):
    name = await asyncio.to_thread(_delete_group, group_id)
    if not name:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True, "name": name}


@app.delete("/api/notifications")
async def delete_notifications(body: DeleteNotificationsRequest, email: str = Depends(auth.require_auth)):
    deleted_by_channel = await asyncio.to_thread(_delete_notifications, body.ids)
    total = 0
    for channel_name, ids in deleted_by_channel.items():
        _broadcast(channel_name, "delete-bulk", {"ids": ids})
        total += len(ids)
    return {"ok": True, "deleted": total}


@app.get("/api/history/{channel_name}")
async def get_history(
    channel_name: str,
    limit: int = 200,
    before_timestamp: Optional[str] = None,
    before_id: Optional[str] = None,
    email: str = Depends(auth.require_auth),
):
    channels = await asyncio.to_thread(_fetch_channels)
    if channel_name not in channels.values():
        raise HTTPException(status_code=404, detail="Channel not found")

    limit = max(1, min(limit, 500))
    before_ts = None
    if before_timestamp:
        try:
            before_ts = datetime.fromisoformat(before_timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid before_timestamp")

    logs, has_more = await asyncio.to_thread(
        _fetch_history, channel_name, limit, before_ts, before_id
    )
    return {"logs": logs, "has_more": has_more}


@app.get("/api/search")
async def search_notifications(
    q: str = "",
    limit: int = 50,
    channel: Optional[str] = None,
    email: str = Depends(auth.require_auth),
):
    query = q.strip()
    if not query:
        return {"results": []}
    limit = max(1, min(limit, 100))
    channel_name = channel.strip() if channel else None
    if channel_name:
        channels = await asyncio.to_thread(_fetch_channels)
        if channel_name not in channels.values():
            raise HTTPException(status_code=404, detail="Channel not found")
    results = await asyncio.to_thread(_search_notifications, query, limit, channel_name)
    return {"results": results}


@app.get("/api/stream/{channel_name}")
async def stream_events(channel_name: str, request: Request, email: str = Depends(auth.require_auth)):
    channels = await asyncio.to_thread(_fetch_channels)
    if channel_name not in channels.values():
        raise HTTPException(status_code=404, detail="Channel not found")

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.setdefault(channel_name, []).append(queue)

    # 2KB 超の SSE コメントでプロキシ（Cloudflare / Apache / nginx）のバッファリングを回避
    _SSE_FLUSH = ":" + " " * 2048 + "\n\n"

    async def generate() -> AsyncIterator[str]:
        try:
            yield _SSE_FLUSH
            yield "event: ping\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield _SSE_FLUSH
                    event = item.get("event", "notification")
                    payload = item.get("data", {})
                    if event == "notification":
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    else:
                        yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield _SSE_FLUSH
                    yield "event: ping\ndata: {}\n\n"
        finally:
            subs = _subscribers.get(channel_name, [])
            if queue in subs:
                subs.remove(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Notification settings ─────────────────────────────────────────────────────

@app.get("/api/notification-settings")
async def get_user_notification_settings(email: str = Depends(auth.require_auth)):
    return await asyncio.to_thread(get_notification_settings, email)


@app.put("/api/channels/{channel_id}/notification-setting")
async def update_channel_notification_setting(
    channel_id: str,
    body: ChannelNotificationSettingRequest,
    email: str = Depends(auth.require_auth),
):
    if "enabled" not in body.model_fields_set:
        raise HTTPException(status_code=400, detail="enabled を指定してください")
    try:
        await asyncio.to_thread(
            set_channel_notification_setting, email, channel_id, body.enabled
        )
    except ValueError as exc:
        if str(exc) == "channel_not_found":
            raise HTTPException(status_code=404, detail="Channel not found")
        raise
    return {"ok": True}


@app.put("/api/groups/{group_id}/notification-setting")
async def update_group_notification_setting(
    group_id: str,
    body: GroupNotificationSettingRequest,
    email: str = Depends(auth.require_auth),
):
    try:
        await asyncio.to_thread(
            set_group_notification_setting, email, group_id, body.enabled
        )
    except ValueError as exc:
        if str(exc) == "group_not_found":
            raise HTTPException(status_code=404, detail="グループが見つかりません")
        raise
    return {"ok": True}


# ── Web Push ──────────────────────────────────────────────────────────────────

class PushSubscribeBody(BaseModel):
    endpoint: str
    keys: dict

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, v: dict) -> dict:
        if not v.get("p256dh") or not v.get("auth"):
            raise ValueError("keys.p256dh と keys.auth が必要です")
        return v


@app.get("/api/push/vapid-public-key")
async def push_vapid_public_key(email: str = Depends(auth.require_auth)):
    if not push_configured():
        raise HTTPException(status_code=503, detail="Web Push が設定されていません")
    ok, err = await asyncio.to_thread(push_vapid_healthy)
    if not ok:
        detail = "VAPID 鍵の読み込みに失敗しました"
        if err and err != "not_configured":
            detail += f"：{err}"
        raise HTTPException(status_code=503, detail=detail)
    public_key = await asyncio.to_thread(get_application_server_key)
    return {"publicKey": public_key}


@app.post("/api/push/subscribe")
async def push_subscribe(body: PushSubscribeBody, email: str = Depends(auth.require_auth)):
    if not push_configured():
        raise HTTPException(status_code=503, detail="Web Push が設定されていません")
    await asyncio.to_thread(
        _upsert_push_subscription,
        email,
        body.endpoint,
        body.keys["p256dh"],
        body.keys["auth"],
    )
    return {"ok": True}


class PushTestBody(BaseModel):
    endpoint: Optional[str] = None
    keys: Optional[dict] = None

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        if not v.get("p256dh") or not v.get("auth"):
            raise ValueError("keys.p256dh と keys.auth が必要です")
        return v


@app.post("/api/push/test")
async def push_test(
    body: PushTestBody = PushTestBody(),
    email: str = Depends(auth.require_auth),
):
    if not push_configured():
        raise HTTPException(status_code=503, detail="Web Push が設定されていません")
    ok, err = await asyncio.to_thread(push_vapid_healthy)
    if not ok:
        detail = "VAPID 鍵の設定に問題があります"
        if err and err != "not_configured":
            detail += f"：{err}"
        raise HTTPException(status_code=503, detail=detail)
    if (
        body.endpoint
        and body.keys
        and body.keys.get("p256dh")
        and body.keys.get("auth")
    ):
        await asyncio.to_thread(
            _upsert_push_subscription,
            email,
            body.endpoint,
            body.keys["p256dh"],
            body.keys["auth"],
        )
    result = await asyncio.to_thread(send_test_push_to_user, email, body.endpoint)
    if result.get("error") == "no_subscription":
        raise HTTPException(status_code=404, detail="Push 登録がありません")
    if result.get("sent", 0) == 0:
        detail = "バックグラウンド通知の送信に失敗しました"
        if result.get("last_status"):
            detail += f"（HTTP {result['last_status']}）"
        if result.get("last_error"):
            detail += f"：{result['last_error']}"
        if result.get("removed"):
            detail += "。「再登録」を試してください"
        elif result.get("last_error") in ("BadJwtToken", "BadAuthorizationHeader"):
            detail += "。「再登録」を試してください"
        else:
            detail += "。「再登録」を試してください"
        raise HTTPException(status_code=502, detail=detail)
    return {"ok": True, **result}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: PushSubscribeBody, email: str = Depends(auth.require_auth)):
    await asyncio.to_thread(_delete_push_subscription, body.endpoint)
    return {"ok": True}


# ── API キー ──────────────────────────────────────────────────────────────────

class CreateApiKeyRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("名前を入力してください")
        if len(v) > 100:
            raise ValueError("名前は100文字以内にしてください")
        return v


@app.get("/api/keys")
async def list_api_keys(email: str = Depends(auth.require_auth)):
    keys = await asyncio.to_thread(_list_api_keys, email)
    return {"keys": keys}


@app.post("/api/keys")
async def create_api_key(body: CreateApiKeyRequest, email: str = Depends(auth.require_auth)):
    created = await asyncio.to_thread(_create_api_key, email, body.name)
    return created


@app.delete("/api/keys/{key_id}")
async def delete_api_key(key_id: str, email: str = Depends(auth.require_auth)):
    deleted = await asyncio.to_thread(_delete_api_key, email, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="API キーが見つかりません")
    return {"ok": True}


@app.get("/docs/webhook.md")
async def get_webhook_docs():
    path = DOCS_DIR / "webhook.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="マニュアルが見つかりません")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@app.get("/docs/api-key.md")
async def get_api_key_docs():
    path = DOCS_DIR / "api-key.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="マニュアルが見つかりません")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


# フロントエンドの静的ファイルを最後にマウント
if FRONTEND_DIR.exists():
    app.mount(
        "/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static"
    )
