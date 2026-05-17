from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from telethon.tl.custom.message import Message


@dataclass(frozen=True)
class FilterResult:
    should_copy: bool
    reason: str
    media_kind: Optional[str] = None


def message_text(message: Message) -> str:
    return message.message or ""


def file_name(message: Message) -> str:
    file = getattr(message, "file", None)
    name = getattr(file, "name", None)
    return name or ""


def media_kind(message: Message) -> Optional[str]:
    if not message.media:
        return None
    if message.video:
        return "video"
    if message.photo:
        return "photo"

    file = getattr(message, "file", None)
    name = (getattr(file, "name", None) or "").lower()
    mime_type = (getattr(file, "mime_type", None) or "").lower()
    suffix = Path(name).suffix.lower()

    if suffix == ".pdf" or mime_type == "application/pdf":
        return "pdf"
    if suffix in {".zip", ".rar", ".7z"} or mime_type in {
        "application/zip",
        "application/x-zip-compressed",
        "application/vnd.rar",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
    }:
        return "zip"
    if message.document:
        return "document"
    return "other"


def matches_keywords(message: Message, keywords: Iterable[str]) -> bool:
    lowered_keywords = [keyword.lower() for keyword in keywords if keyword]
    if not lowered_keywords:
        return True

    haystack = " ".join(
        part for part in [message_text(message), file_name(message)] if part
    ).lower()
    return any(keyword in haystack for keyword in lowered_keywords)


def should_copy_message(message: Message, config) -> FilterResult:
    kind = media_kind(message)
    keyword_filter_enabled = bool(config.keywords)
    keyword_match = matches_keywords(message, config.keywords)

    if kind is None:
        if not message_text(message).strip():
            return FilterResult(False, "empty message", kind)
        if not config.include_text_only_keyword_posts:
            return FilterResult(False, "text-only posts disabled", kind)
        if not keyword_match:
            return FilterResult(False, "text did not match keywords", kind)
        reason = "text matched keywords" if keyword_filter_enabled else "text post"
        return FilterResult(True, reason, kind)

    if kind not in config.allowed_media:
        return FilterResult(False, f"media kind not allowed: {kind}", kind)
    if not keyword_match:
        return FilterResult(False, "media post did not match keywords", kind)
    reason = f"{kind} matched keywords" if keyword_filter_enabled else f"{kind} post"
    return FilterResult(True, reason, kind)


def selected_album_messages(messages: list[Message], config) -> tuple[list[Message], str]:
    keyword_filter_enabled = bool(config.keywords)
    keyword_match = any(matches_keywords(message, config.keywords) for message in messages)
    if not keyword_match:
        return [], "album did not match keywords"

    selected = [
        message
        for message in messages
        if (kind := media_kind(message)) is not None and kind in config.allowed_media
    ]
    if not selected:
        return [], "album had no allowed media"
    reason = "album matched keywords" if keyword_filter_enabled else "album"
    return selected, reason
