from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Optional, TypeVar, Union

from telethon.errors import FloodWaitError

T = TypeVar("T")


def setup_logging(log_file: Path, extra_handlers: Optional[list[logging.Handler]] = None) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("telegram_backfill")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    for handler in extra_handlers or []:
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


async def cancellable_sleep(seconds: float, cancel_event=None) -> None:
    remaining = seconds
    while remaining > 0:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        interval = min(0.5, remaining)
        await asyncio.sleep(interval)
        remaining -= interval


async def with_retries(
    description: str,
    action: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    logger: logging.Logger,
    cancel_event=None,
) -> T:
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        try:
            return await action()
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            wait_seconds = int(exc.seconds) + 1
            logger.warning("%s hit FloodWait; sleeping %s seconds", description, wait_seconds)
            await cancellable_sleep(wait_seconds, cancel_event)
            last_error = exc
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            wait_seconds = min(60, 2 ** attempt)
            logger.warning(
                "RETRY %s failed on attempt %s/%s: %s; retrying in %s seconds",
                description,
                attempt,
                attempts,
                exc,
                wait_seconds,
            )
            await cancellable_sleep(wait_seconds, cancel_event)

    assert last_error is not None
    raise last_error


def ensure_temp_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def cleanup_paths(paths: Iterable[Union[str, Path]]) -> None:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def normalize_sent_ids(sent) -> list[int]:
    if sent is None:
        return []
    if isinstance(sent, list):
        return [item.id for item in sent if item is not None and hasattr(item, "id")]
    return [sent.id] if hasattr(sent, "id") else []
