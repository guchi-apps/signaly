"""Webhook ペイロードのパース（Discord 形式 + Signaly レガシー形式）"""

from typing import Any, Dict, List, Optional

# 送信元（source）を明示するリクエストヘッダー。
# 用途別に統合したチャンネルの中で、どのアプリから来た通知かを見分けるために使う。
SOURCE_HEADER = "x-signaly-source"

MAX_SOURCE_LEN = 100

# CI / デプロイ通知（.github/scripts/signaly-notify.sh）が embed fields に載せている
# 送信元を表すフィールド名。前にあるものほど優先する。
_SOURCE_FIELD_NAMES = ("app", "repository", "repo")

# Discord Execute Webhook で使われるトップレベルキー
_DISCORD_KEYS = frozenset({
    "content",
    "embeds",
    "username",
    "avatar_url",
    "tts",
    "allowed_mentions",
    "components",
    "attachments",
    "flags",
    "thread_name",
    "applied_tags",
    "poll",
})


def discord_color_to_hex(color: Any) -> Optional[str]:
    """Discord embed color（10進数）を CSS hex に変換する。"""
    if color is None:
        return None
    try:
        value = int(color)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 0xFFFFFF:
        return None
    return f"#{value:06x}"


def normalize_source(value: Any) -> Optional[str]:
    """送信元として保存できる文字列に整える。空・非スカラーなら None。"""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:MAX_SOURCE_LEN]


def _source_from_fields(fields: List[dict]) -> Optional[str]:
    """パース済みフィールドから送信元らしい値を拾う。

    CI 通知は `App` に `signaly` のようなアプリ名、`Repository` に `` `guchi-apps/signaly` ``
    を入れている。App が無いリポジトリでも Repository から拾えるよう両方見る。
    """
    by_name = {}
    for field in fields:
        name = str(field.get("name") or "").strip().lower()
        if name and name not in by_name:
            by_name[name] = field.get("value")
    for key in _SOURCE_FIELD_NAMES:
        source = normalize_source(by_name.get(key))
        if source:
            # Repository は `guchi-apps/signaly` のようにバッククォートで囲まれている
            source = source.strip("`")
            return source.split("/")[-1] if key != "app" else source
    return None


def is_discord_payload(data: dict) -> bool:
    return bool(_DISCORD_KEYS & data.keys())


def _normalize_newlines(text: str) -> str:
    """JSON / shell 由来のリテラル \\n を実際の改行に直す。"""
    return text.replace("\\n", "\n")


def _embed_author_field(author: dict) -> Optional[dict]:
    name = author.get("name")
    if not name:
        return None
    url = author.get("url")
    value = f"[{name}]({url})" if url else name
    return {"name": "Author", "value": value, "inline": True}


def _embed_footer_field(footer: dict) -> Optional[dict]:
    text = footer.get("text")
    if not text:
        return None
    return {"name": "\u200b", "value": text, "inline": False}


def _embed_image_field(label: str, image: dict) -> Optional[dict]:
    url = image.get("url")
    if not url or url.startswith("attachment://"):
        return None
    return {"name": label, "value": url, "inline": False}


def _split_content_title_body(content: str) -> tuple[str, str]:
    """content の 1 行目をタイトル、残りを本文として分離する。"""
    text = content.strip()
    if not text:
        return "", ""
    if "\n" not in text:
        return text, ""
    first, _, rest = text.partition("\n")
    return first.strip(), rest.strip()


def parse_discord_payload(data: dict) -> dict:
    """Discord Execute Webhook 形式を Signaly 内部形式に変換する。"""
    content = _normalize_newlines((data.get("content") or "").strip())
    embeds: List[dict] = data.get("embeds") or []
    username = data.get("username")

    title = ""
    message_parts: List[str] = []
    color: Optional[str] = None
    fields: List[dict] = []

    if content:
        if embeds:
            # embed があるときは Discord と同様 content 全文を本文に含める
            message_parts.append(content)
        else:
            content_title, content_body = _split_content_title_body(content)
            if content_body:
                title = content_title
                message_parts.append(content_body)
            else:
                title = content_title

    for i, embed in enumerate(embeds):
        if not isinstance(embed, dict):
            continue

        if not title and embed.get("title"):
            title = str(embed["title"])

        description = (embed.get("description") or "").strip()
        if description:
            message_parts.append(description)

        if color is None and embed.get("color") is not None:
            color = discord_color_to_hex(embed["color"])

        author = embed.get("author")
        if isinstance(author, dict):
            author_field = _embed_author_field(author)
            if author_field:
                fields.append(author_field)

        for field in embed.get("fields") or []:
            if not isinstance(field, dict):
                continue
            fields.append({
                "name": str(field.get("name") or ""),
                "value": str(field.get("value") or ""),
                "inline": bool(field.get("inline", False)),
            })

        footer = embed.get("footer")
        if isinstance(footer, dict):
            footer_field = _embed_footer_field(footer)
            if footer_field:
                fields.append(footer_field)

        thumbnail = embed.get("thumbnail")
        if isinstance(thumbnail, dict):
            thumb_field = _embed_image_field("Thumbnail", thumbnail)
            if thumb_field:
                fields.append(thumb_field)

        image = embed.get("image")
        if isinstance(image, dict):
            image_field = _embed_image_field("Image", image)
            if image_field:
                fields.append(image_field)

        embed_url = embed.get("url")
        if embed_url and title and i == 0:
            title = f"[{title}]({embed_url})"

    if not title and username:
        title = str(username)

    # 送信元の優先順: ペイロードの source → embed fields の App / Repository → username。
    # CI 通知は App を必ず載せるため、送信側を変えずに送信元が付く。
    source = (
        normalize_source(data.get("source"))
        or _source_from_fields(fields)
        or normalize_source(username)
    )

    return {
        "title": title,
        "message": "\n\n".join(message_parts),
        "level": "info",
        "color": color,
        "fields": fields or None,
        "source": source,
    }


def parse_legacy_payload(data: dict) -> dict:
    """Signaly レガシー形式を内部形式に正規化する。"""
    fields = data.get("fields")
    fields_list = [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []
    return {
        "title": data.get("title") or "",
        "message": data.get("message") or "",
        "level": data.get("level") or "info",
        "color": data.get("color"),
        "fields": fields,
        "source": normalize_source(data.get("source")) or _source_from_fields(fields_list),
    }


def parse_webhook_payload(data: dict) -> dict:
    """リクエスト JSON を Signaly 内部形式に変換する。"""
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    if is_discord_payload(data):
        return parse_discord_payload(data)
    return parse_legacy_payload(data)
