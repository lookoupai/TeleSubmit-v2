"""Extract a bounded, indexable representation of Telegram content."""

import json
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
MAX_BYTES = 64 * 1024
MAX_DEPTH = 16
BLOCKS = frozenset(
    {
        "paragraph",
        "heading",
        "pre",
        "footer",
        "divider",
        "anchor",
        "list",
        "blockquote",
        "expandable_blockquote",
        "pullquote",
        "table",
        "details",
        "collage",
        "slideshow",
        "map",
        "buttons",
        "document",
        "audio",
        "animation",
        "photo",
        "video",
        "voice_note",
        "mathematical_expression",
    }
)


def telegram_field(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    if obj is None:
        return None
    return getattr(obj, key, None) or (getattr(obj, "api_kwargs", None) or {}).get(key)


def web_url(value):
    if (
        isinstance(value, str)
        and len(value) <= 2048
        and not any(c.isspace() for c in value)
    ):
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and not parsed.username
            ):
                return value
        except ValueError:
            pass
    return ""


@dataclass
class NormalizedContent:
    text: str = ""
    rich: bool = False
    links: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    filenames: list = field(default_factory=list)
    quote: str = ""

    def source_json(self):
        return json.dumps(vars(self), ensure_ascii=False)


def normalize_message_content(message):
    rich = telegram_field(message, "rich_message")
    result = NormalizedContent(rich=isinstance(rich, dict))
    remaining = MAX_BYTES
    nodes = 4096
    unknown = set()

    def text(value):
        nonlocal remaining
        if not isinstance(value, str) or not remaining:
            return ""
        data = value[:remaining].encode("utf-8")[:remaining]
        remaining -= len(data)
        return data.decode("utf-8", errors="ignore")

    def extract(value, depth=0, *, blocks=False, separator=""):
        nonlocal nodes
        nodes -= 1
        if depth > MAX_DEPTH or not remaining or nodes < 0:
            return ""
        if isinstance(value, str):
            return text(value)
        if isinstance(value, (tuple, list)):
            parts = []
            for item in value:
                if nodes <= 0 or not remaining:
                    break
                part = extract(item, depth + 1, blocks=blocks, separator=separator)
                if part:
                    parts.append(part)
            return separator.join(parts)
        if not isinstance(value, dict):
            return ""

        kind = value.get("type")
        if blocks and (not isinstance(kind, str) or kind not in BLOCKS):
            if isinstance(kind, str) and len(unknown) < 12:
                unknown.add(kind[:48])
            return ""
        parts = []
        for key in (
            "summary",
            "label",
            "text",
            "caption",
            "credit",
            "expression",
            "url",
            "blocks",
            "items",
            "cells",
        ):
            if key == "url":
                url = web_url(value.get(key))
                if url and url not in result.links:
                    url = text(url)
                    if url:
                        result.links.append(url)
                continue
            part = (
                extract(
                    value.get(key),
                    depth + 1,
                    blocks=key == "blocks",
                    separator="\n" if key in {"blocks", "items", "cells"} else "",
                )
                if key in value
                else ""
            )
            if part:
                parts.append(part)

        if blocks and kind in {"document", "audio", "animation", "video"}:
            file = value.get(kind)
            if isinstance(file, dict):
                name = text(file.get("file_name"))
                if name and name not in result.filenames:
                    result.filenames.append(name)
                file_id = file.get("file_id")
                if (
                    kind == "document"
                    and isinstance(file_id, str)
                    and file_id
                    and len(file_id) <= 512
                ):
                    result.documents.append((file_id, name))
        return "\n".join(parts)

    paragraphs = (
        extract(rich.get("blocks", []), blocks=True, separator="\n")
        if result.rich
        else ""
    )
    if unknown:
        logger.debug("未识别的富消息块类型: %r", sorted(unknown))
    ordinary = telegram_field(message, "caption") or telegram_field(message, "text")
    result.text = paragraphs.strip() if paragraphs.strip() else text(ordinary).strip()
    return result


def normalized_quote(message):
    quote = telegram_field(message, "quote")
    rich_texts = telegram_field(quote, "rich_texts")
    return normalize_message_content(
        {
            "text": telegram_field(quote, "text"),
            "rich_message": (
                {"blocks": [{"type": "paragraph", "text": rich_texts}]}
                if rich_texts
                else None
            ),
        }
    ).text


def search_description(source_json, fallback):
    if not source_json:
        return fallback or ""
    source = json.loads(source_json)
    text = source.get("text", "")
    extras = [
        v
        for v in [*source.get("links", []), *source.get("filenames", [])]
        if v not in text
    ]
    return "\n".join([text, *extras]).strip()
