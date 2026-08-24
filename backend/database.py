import os
from urllib.parse import quote

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session

_DB_USER = os.environ.get("DB_USER", "user")
_DB_PASSWORD = os.environ.get("DB_PASSWORD", "password")
_DB_HOST = os.environ.get("DB_HOST", "localhost")
_DB_PORT = os.environ.get("DB_PORT", "3306")
_DB_NAME = os.environ["DB_NAME"]


def _build_engine(user: str, password: str):
    return create_engine(
        f"mysql+pymysql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}?charset=utf8mb4",
        pool_pre_ping=True,
    )


# アプリ本体が使うエンジン。VPS 共通のアプリ用ユーザーは SELECT / INSERT / UPDATE /
# DELETE しか持たず、CREATE TABLE や ALTER TABLE は実行できない。
engine = _build_engine(_DB_USER, _DB_PASSWORD)


def ddl_engine():
    """DDL（CREATE TABLE / ALTER TABLE）を流すためのエンジンを返す。

    本番の DB ユーザーは用途で分かれている（guchi-apps/vps の `docs/web-stack.md`）。
    常時稼働するアプリ用ユーザーは CRUD 権限のみで、DDL 権限はマイグレーション専用
    ユーザーだけが持つ。`DB_ADMIN_USER` / `DB_ADMIN_PASSWORD` はそのマイグレーション用
    ユーザーで、デプロイ時のマイグレーション（`backend/migrate_db.py`）の実行中だけ
    環境変数として渡す。**`.env` へは書かないこと**——アプリ本体まで DDL 権限を持つ。

    未設定なら（ローカルなど、1つのユーザーが DDL まで持つ環境向けに）アプリ用の
    エンジンへフォールバックする。
    """
    admin_user = os.environ.get("DB_ADMIN_USER")
    admin_password = os.environ.get("DB_ADMIN_PASSWORD", "")
    if not admin_user:
        return engine, False
    return _build_engine(admin_user, admin_password), True


class Base(DeclarativeBase):
    pass


class ChannelGroup(Base):
    __tablename__ = "channel_groups"

    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class Channel(Base):
    __tablename__ = "channels"

    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    group_id = Column(
        String(36),
        ForeignKey("channel_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sort_order = Column(Integer, nullable=False, default=0)
    webhook_secret_hash = Column(String(64), nullable=True)
    webhook_secret_enc = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class ChannelAlias(Base):
    """統合で消えたチャンネルIDを、統合先チャンネルへ転送するための別名。

    チャンネルを別チャンネルへ統合すると旧チャンネルの行は消えるが、旧チャンネルIDは
    各リポジトリの1Password / GitHub secret に Webhook URL として散らばっている。
    ここへ旧IDを残すことで `/webhook/{旧ID}` が統合先へ届き続け、送信側の差し替えが要らない。
    """

    __tablename__ = "channel_aliases"

    id = Column(String(36), primary_key=True)  # 旧チャンネルID（＝Webhook URL のパス）
    channel_id = Column(
        String(36),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source = Column(String(100), nullable=True)  # このIDで届いた通知に付ける送信元名
    created_at = Column(DateTime(timezone=True), nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(36), primary_key=True)
    email = Column(String(255), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    key_hash = Column(String(64), nullable=False, unique=True)
    key_prefix = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String(36), primary_key=True)
    channel = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False, default="")
    message = Column(Text, nullable=False)
    level = Column(String(20), nullable=False, default="info")
    timestamp = Column(DateTime(timezone=True), nullable=False)
    fields = Column(Text, nullable=True)   # JSON array [{name, value, inline}]
    color = Column(String(20), nullable=True)  # CSS hex color e.g. #57f287
    # 送信元（アプリ名・リポジトリ名など）。用途別に統合したチャンネルの中で発信元を見分ける。
    # 過去の行は NULL のままなので、参照側は必ず未設定を許容すること。
    source = Column(String(100), nullable=True, index=True)


class NotificationSetting(Base):
    __tablename__ = "notification_settings"

    email = Column(String(255), primary_key=True, nullable=False)
    target_type = Column(String(10), primary_key=True, nullable=False)
    target_id = Column(String(36), primary_key=True, nullable=False)
    enabled = Column(Boolean, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(String(36), primary_key=True)
    email = Column(String(255), nullable=False, index=True)
    endpoint_hash = Column(String(64), nullable=False, unique=True)
    endpoint = Column(Text, nullable=False)
    p256dh = Column(String(255), nullable=False)
    auth = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


def get_session() -> Session:
    return Session(engine)


def _migrate_add_columns(bind=None) -> None:
    notification_columns = [
        "fields TEXT NULL",
        "color VARCHAR(20) NULL",
        "source VARCHAR(100) NULL",
    ]
    channel_columns = [
        "webhook_secret_hash VARCHAR(64) NULL",
        "webhook_secret_enc TEXT NULL",
        "group_id VARCHAR(36) NULL",
        "sort_order INT NOT NULL DEFAULT 0",
    ]
    with (bind or engine).connect() as conn:
        for col_def in notification_columns:
            try:
                conn.execute(text(f"ALTER TABLE notifications ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass  # column already exists
        for col_def in channel_columns:
            try:
                conn.execute(text(f"ALTER TABLE channels ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass  # column already exists
        # create_all はテーブルを新規に作るときしかインデックスを張らない。
        # 既存の notifications へ後から足した列には、ここで明示的に張る。
        try:
            conn.execute(text("CREATE INDEX ix_notifications_source ON notifications (source)"))
            conn.commit()
        except Exception:
            pass  # index already exists


def init_db() -> None:
    """テーブルの作成と列の追加を行う。

    **アプリの起動時には呼ばないこと。** アプリ用の DB ユーザーは DDL 権限を持たない
    ため、テーブルが1つでも増えると起動のたびに `CREATE command denied` で落ちる
    （#183）。呼び出し口はデプロイ時に走る `backend/migrate_db.py` だけにする。
    """
    bind, is_admin = ddl_engine()
    try:
        Base.metadata.create_all(bind=bind)
        _migrate_add_columns(bind)
    finally:
        if is_admin:
            bind.dispose()
