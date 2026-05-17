from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional


DEFAULT_KEYWORDS: list[str] = []
DEFAULT_ALLOWED_MEDIA = ["video", "pdf", "zip", "document", "photo", "other"]


def _split_csv(value: Optional[str], default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _read_message_links_file(path_value: Optional[str]) -> list[str]:
    if not path_value:
        return []

    path = Path(path_value).expanduser()
    if not path.exists():
        raise ValueError(f"Message links file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Message links path is not a file: {path}")

    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(value: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _limit_from_arg_env(arg_value: Optional[int], env_name: str) -> Optional[int]:
    if arg_value is not None:
        return arg_value if arg_value > 0 else None
    return _optional_int(os.getenv(env_name))


def parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse an ISO date/datetime and normalize it to UTC."""
    raw = value.strip()
    if len(raw) == 10:
        date = datetime.fromisoformat(raw).date()
        dt = datetime.combine(date, time.max if end_of_day else time.min)
    else:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    source_channel: str
    target_channel: str
    start_date: datetime
    end_date: datetime
    keywords: list[str]
    allowed_media: set[str]
    include_photos: bool
    include_text_only_keyword_posts: bool
    scan_limit: Optional[int]
    send_limit: Optional[int]
    message_links: list[str]
    dry_run: bool
    session_name: str
    database_path: Path
    temp_dir: Path
    log_file: Path
    retry_attempts: int
    send_delay_seconds: float
    history_wait_seconds: float

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "AppConfig":
        api_id = args.api_id or os.getenv("TG_API_ID")
        api_hash = args.api_hash or os.getenv("TG_API_HASH")
        source_channel = args.source or os.getenv("TG_SOURCE_CHANNEL")
        target_channel = args.target or os.getenv("TG_TARGET_CHANNEL")
        message_links = (
            _split_csv(args.message_links, [])
            + _read_message_links_file(args.message_links_file)
        )

        required = {
            "api_id/TG_API_ID": api_id,
            "api_hash/TG_API_HASH": api_hash,
            "target/TG_TARGET_CHANNEL": target_channel,
        }
        if not message_links:
            required["source/TG_SOURCE_CHANNEL"] = source_channel
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("Missing required configuration: " + ", ".join(missing))

        raw_keywords = args.keywords if args.keywords is not None else os.getenv("KEYWORDS")
        raw_allowed_media = (
            args.allowed_media if args.allowed_media is not None else os.getenv("ALLOWED_MEDIA")
        )

        allowed_media = set(
            item.lower()
            for item in _split_csv(raw_allowed_media, DEFAULT_ALLOWED_MEDIA)
        )
        if args.include_photos or _env_bool("INCLUDE_PHOTOS", False):
            allowed_media.add("photo")

        return cls(
            api_id=int(api_id),
            api_hash=str(api_hash),
            source_channel=str(source_channel or ""),
            target_channel=str(target_channel),
            start_date=parse_date(args.start_date or os.getenv("START_DATE", "2024-01-01")),
            end_date=parse_date(args.end_date or os.getenv("END_DATE", "2026-05-15"), end_of_day=True),
            keywords=_split_csv(raw_keywords, DEFAULT_KEYWORDS),
            allowed_media=allowed_media,
            include_photos="photo" in allowed_media,
            include_text_only_keyword_posts=(
                not args.exclude_text_only
                and (
                    args.include_text_only_keyword_posts
                    or _env_bool("INCLUDE_TEXT_ONLY_KEYWORD_POSTS", True)
                )
            ),
            scan_limit=_limit_from_arg_env(args.scan_limit, "SCAN_LIMIT"),
            send_limit=_limit_from_arg_env(args.send_limit, "SEND_LIMIT"),
            message_links=message_links,
            dry_run=not args.execute,
            session_name=args.session_name or os.getenv("TG_SESSION_NAME", "telegram_backfill"),
            database_path=Path(args.database or os.getenv("DATABASE_PATH", "processed.sqlite3")),
            temp_dir=Path(args.temp_dir or os.getenv("TEMP_DIR", "tmp_downloads")),
            log_file=Path(args.log_file or os.getenv("LOG_FILE", "copy.log")),
            retry_attempts=args.retry_attempts,
            send_delay_seconds=args.send_delay,
            history_wait_seconds=args.history_wait,
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy Telegram channel history to your own channel without forwarding."
    )
    parser.add_argument("--api-id", type=int, help="Telegram API ID. Can also use TG_API_ID.")
    parser.add_argument("--api-hash", help="Telegram API hash. Can also use TG_API_HASH.")
    parser.add_argument("--source", help="Source public channel username or link. Can also use TG_SOURCE_CHANNEL.")
    parser.add_argument("--target", help="Target channel username or link. Can also use TG_TARGET_CHANNEL.")
    parser.add_argument("--start-date", default="2024-01-01", help="Inclusive UTC start date/date-time.")
    parser.add_argument("--end-date", default="2026-05-15", help="Inclusive UTC end date/date-time.")
    parser.add_argument(
        "--keywords",
        help="Optional comma-separated keywords. Omit or pass an empty string to copy everything.",
    )
    parser.add_argument(
        "--allowed-media",
        default=None,
        help="Optional comma-separated media kinds to copy: video,pdf,zip,document,photo,other.",
    )
    parser.add_argument(
        "--include-photos",
        action="store_true",
        help="Allow photo posts and photo album items. Photos are already allowed by default.",
    )
    parser.add_argument(
        "--include-text-only-keyword-posts",
        action="store_true",
        help="Deprecated compatibility flag. Text-only posts are already included by default.",
    )
    parser.add_argument(
        "--exclude-text-only",
        action="store_true",
        help="Optional filter: skip text-only posts.",
    )
    parser.add_argument("--scan-limit", type=int, help="Maximum source messages to scan. 0 or omitted means unlimited.")
    parser.add_argument("--send-limit", type=int, help="Maximum matching source messages to send. 0 or omitted means unlimited.")
    parser.add_argument(
        "--message-links",
        help="Optional comma-separated Telegram message links. When set, only those messages are copied.",
    )
    parser.add_argument(
        "--message-links-file",
        help="Optional text file with one Telegram message link per line.",
    )
    parser.add_argument("--execute", action="store_true", help="Actually send messages. Default is dry-run.")
    parser.add_argument("--session-name", help="Telethon session name/file prefix.")
    parser.add_argument("--database", help="SQLite database path.")
    parser.add_argument("--temp-dir", help="Temporary media download directory.")
    parser.add_argument("--log-file", help="Log file path.")
    parser.add_argument("--retry-attempts", type=int, default=8, help="Retries for downloads/uploads.")
    parser.add_argument("--send-delay", type=float, default=1.5, help="Delay between copied posts/albums.")
    parser.add_argument("--history-wait", type=float, default=1.0, help="Wait time between history requests.")
    return parser
