from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Optional

from telethon.tl.custom.message import Message

from config import AppConfig, build_arg_parser
from database import ProcessedDatabase
from filters import file_name, selected_album_messages, should_copy_message
from telegram_client import create_client, iter_history_oldest_first
from utils import cleanup_paths, ensure_temp_dir, normalize_sent_ids, setup_logging, with_retries


@dataclass
class Counters:
    scanned: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0


MESSAGE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/\d+/|[^/]+/)(\d+)(?:[/?#].*)?$")
ProgressCallback = Callable[[dict], None]


class ProgressReporter:
    def __init__(
        self,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[Event] = None,
        min_interval: float = 0.5,
    ) -> None:
        self.callback = callback
        self.cancel_event = cancel_event
        self.min_interval = min_interval
        self._last_emit_by_key: dict[str, float] = {}
        self._start_by_key: dict[str, tuple[float, int]] = {}

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise asyncio.CancelledError()

    def emit(self, **payload) -> None:
        self.check_cancelled()
        if self.callback is not None:
            self.callback(payload)

    def counters(self, counters: Counters, status: Optional[str] = None) -> None:
        payload = {
            "scanned": counters.scanned,
            "copied": counters.copied,
            "skipped": counters.skipped,
            "failed": counters.failed,
        }
        if status:
            payload["status"] = status
        self.emit(**payload)

    def progress_callback(self, status: str, message: Message, filename: str):
        key = f"{status}:{message.id}:{filename}"
        self._start_by_key[key] = (time.monotonic(), 0)

        def _callback(current: int, total: int) -> None:
            self.check_cancelled()
            now = time.monotonic()
            last_emit = self._last_emit_by_key.get(key, 0.0)
            if total and current < total and now - last_emit < self.min_interval:
                return

            start_time, start_bytes = self._start_by_key.get(key, (now, current))
            elapsed = max(now - start_time, 0.001)
            speed = max(current - start_bytes, 0) / elapsed / (1024 * 1024)
            percent = int((current / total) * 100) if total else 0
            self._last_emit_by_key[key] = now
            self.emit(
                status=status,
                source_id=message.id,
                filename=filename,
                current_bytes=current,
                total_bytes=total,
                percent=percent,
                speed_mbps=speed,
            )

        return _callback


def display_filename(message: Message, fallback: str = "media") -> str:
    return file_name(message) or fallback


async def download_message_media(
    client,
    message: Message,
    config: AppConfig,
    logger: logging.Logger,
    progress: ProgressReporter,
) -> str:
    ensure_temp_dir(config.temp_dir)
    filename = display_filename(message, f"source_{message.id}")
    logger.info("DOWNLOADING source_id=%s filename=%s", message.id, filename)

    async def _download() -> str:
        path = await client.download_media(
            message,
            file=str(config.temp_dir),
            progress_callback=progress.progress_callback("downloading", message, filename),
        )
        if not path:
            raise RuntimeError(f"download returned no path for source message {message.id}")
        return str(path)

    return await with_retries(
        f"download source message {message.id}",
        _download,
        attempts=config.retry_attempts,
        logger=logger,
        cancel_event=progress.cancel_event,
    )


async def send_text(
    client,
    target,
    message: Message,
    config: AppConfig,
    logger: logging.Logger,
    progress: ProgressReporter,
) -> list[int]:
    progress.emit(status="uploading", source_id=message.id, filename="text")

    async def _send():
        return await client.send_message(
            target,
            message.message or "",
            formatting_entities=message.entities,
            link_preview=True,
        )

    sent = await with_retries(
        f"send text source message {message.id}",
        _send,
        attempts=config.retry_attempts,
        logger=logger,
        cancel_event=progress.cancel_event,
    )
    return normalize_sent_ids(sent)


async def send_media(
    client,
    target,
    message: Message,
    config: AppConfig,
    logger: logging.Logger,
    progress: ProgressReporter,
) -> list[int]:
    paths: list[str] = []
    try:
        media_path = await download_message_media(client, message, config, logger, progress)
        paths.append(media_path)
        upload_name = Path(media_path).name
        logger.info("UPLOADING source_id=%s filename=%s", message.id, upload_name)

        async def _send():
            return await client.send_file(
                target,
                media_path,
                caption=message.message or None,
                formatting_entities=message.entities,
                force_document=not bool(message.video or message.photo),
                supports_streaming=bool(message.video),
                progress_callback=progress.progress_callback("uploading", message, upload_name),
            )

        sent = await with_retries(
            f"upload source message {message.id}",
            _send,
            attempts=config.retry_attempts,
            logger=logger,
            cancel_event=progress.cancel_event,
        )
        return normalize_sent_ids(sent)
    finally:
        cleanup_paths(paths)


async def send_album(
    client,
    target,
    messages: list[Message],
    config: AppConfig,
    logger: logging.Logger,
    progress: ProgressReporter,
) -> list[int]:
    sent_ids: list[int] = []
    paths: list[str] = []
    try:
        for message in messages:
            paths.append(await download_message_media(client, message, config, logger, progress))

        for index in range(0, len(paths), 10):
            chunk_paths = paths[index : index + 10]
            chunk_messages = messages[index : index + 10]
            captions = [message.message or "" for message in chunk_messages]

            async def _send_chunk():
                if len(chunk_paths) == 1:
                    only_message = chunk_messages[0]
                    upload_name = Path(chunk_paths[0]).name
                    logger.info("UPLOADING source_id=%s filename=%s", only_message.id, upload_name)
                    return await client.send_file(
                        target,
                        chunk_paths[0],
                        caption=only_message.message or None,
                        formatting_entities=only_message.entities,
                        force_document=not bool(only_message.video or only_message.photo),
                        supports_streaming=bool(only_message.video),
                        progress_callback=progress.progress_callback("uploading", only_message, upload_name),
                    )
                upload_name = f"album chunk {index // 10 + 1}"
                logger.info(
                    "UPLOADING album source_ids=%s filename=%s",
                    [message.id for message in chunk_messages],
                    upload_name,
                )
                return await client.send_file(
                    target,
                    chunk_paths,
                    caption=captions,
                    # Telethon can raise "Subscripted generics cannot be used
                    # with class and instance checks" for album entity lists.
                    # Keep captions for album items, but let Telethon parse
                    # them instead of passing per-item formatting entities.
                    force_document=False,
                    progress_callback=progress.progress_callback("uploading", chunk_messages[0], upload_name),
                )

            sent = await with_retries(
                "upload album " + ",".join(str(message.id) for message in chunk_messages),
                _send_chunk,
                attempts=config.retry_attempts,
                logger=logger,
                cancel_event=progress.cancel_event,
            )
            sent_ids.extend(normalize_sent_ids(sent))
    finally:
        cleanup_paths(paths)

    return sent_ids


async def process_single_message(
    client,
    target,
    message: Message,
    config: AppConfig,
    db: ProcessedDatabase,
    logger: logging.Logger,
    counters: Counters,
    progress: ProgressReporter,
    force_recopy: bool = False,
) -> None:
    progress.emit(status="scanning", source_id=message.id, filename=display_filename(message, "text"))
    if db.is_copied(config.source_channel, message.id) and not force_recopy:
        counters.skipped += 1
        logger.info("SKIP already copied source_id=%s", message.id)
        progress.counters(counters, "skipped")
        return

    result = should_copy_message(message, config)
    if not result.should_copy:
        counters.skipped += 1
        if not config.dry_run:
            db.mark_skipped(config.source_channel, message.id, result.reason)
        logger.info("SKIP source_id=%s reason=%s", message.id, result.reason)
        progress.counters(counters, "skipped")
        return

    if config.send_limit is not None and counters.copied >= config.send_limit:
        return

    if config.dry_run:
        counters.copied += 1
        logger.info("DRY-RUN copy source_id=%s reason=%s", message.id, result.reason)
        progress.counters(counters, "copied")
        return

    try:
        if message.media:
            target_ids = await send_media(client, target, message, config, logger, progress)
        else:
            target_ids = await send_text(client, target, message, config, logger, progress)
        db.mark_copied(config.source_channel, [message.id], target_ids)
        counters.copied += 1
        logger.info("COPIED source_id=%s target_ids=%s", message.id, target_ids)
        progress.counters(counters, "copied")
        await asyncio.sleep(config.send_delay_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        counters.failed += 1
        db.mark_failed(config.source_channel, message.id, str(exc))
        logger.exception("FAILED source_id=%s error=%s", message.id, exc)
        progress.counters(counters, "failed")


async def process_album(
    client,
    target,
    messages: list[Message],
    config: AppConfig,
    db: ProcessedDatabase,
    logger: logging.Logger,
    counters: Counters,
    progress: ProgressReporter,
    force_recopy: bool = False,
) -> None:
    if not messages:
        return

    source_ids = [message.id for message in messages]
    progress.emit(status="scanning", source_id=source_ids[0], filename="album")
    uncopied_messages = messages
    if not force_recopy:
        copied_ids = {
            message_id
            for message_id in source_ids
            if db.is_copied(config.source_channel, message_id)
        }
        if len(copied_ids) == len(source_ids):
            counters.skipped += len(source_ids)
            logger.info("SKIP already copied album source_ids=%s", source_ids)
            progress.counters(counters, "skipped")
            return

        uncopied_messages = [message for message in messages if message.id not in copied_ids]
        if copied_ids:
            counters.skipped += len(copied_ids)
            logger.info("SKIP already copied album items source_ids=%s", sorted(copied_ids))
            progress.counters(counters, "skipped")

    selected, reason = selected_album_messages(uncopied_messages, config)
    if not selected:
        counters.skipped += len(uncopied_messages)
        if not config.dry_run:
            for message in uncopied_messages:
                db.mark_skipped(config.source_channel, message.id, reason)
        logger.info("SKIP album source_ids=%s reason=%s", source_ids, reason)
        progress.counters(counters, "skipped")
        return

    selected_ids = [message.id for message in selected]
    if config.send_limit is not None and counters.copied + len(selected_ids) > config.send_limit:
        logger.info("STOP send limit would split album source_ids=%s", selected_ids)
        return

    if config.dry_run:
        counters.copied += len(selected_ids)
        logger.info("DRY-RUN copy album source_ids=%s reason=%s", selected_ids, reason)
        progress.counters(counters, "copied")
        return

    try:
        target_ids = await send_album(client, target, selected, config, logger, progress)
        db.mark_copied(
            config.source_channel,
            selected_ids,
            target_ids,
            grouped_id=messages[0].grouped_id,
        )
        counters.copied += len(selected_ids)
        skipped_unselected = {message.id for message in uncopied_messages} - set(selected_ids)
        for message_id in skipped_unselected:
            db.mark_skipped(config.source_channel, message_id, "album item did not match allowed media")
        logger.info("COPIED album source_ids=%s target_ids=%s", selected_ids, target_ids)
        progress.counters(counters, "copied")
        await asyncio.sleep(config.send_delay_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        counters.failed += len(selected_ids)
        for message_id in selected_ids:
            db.mark_failed(config.source_channel, message_id, str(exc))
        logger.exception("FAILED album source_ids=%s error=%s", selected_ids, exc)
        progress.counters(counters, "failed")


def parse_message_id(link: str) -> int:
    raw = link.strip()
    match = MESSAGE_LINK_RE.match(raw)
    if match:
        return int(match.group(1))

    if raw.isdigit():
        return int(raw)

    raise ValueError(f"Could not parse Telegram message ID from link: {link}")


async def fetch_message_album(client, source, message: Message) -> list[Message]:
    if not message.grouped_id:
        return [message]

    start_id = max(1, message.id - 15)
    end_id = message.id + 15
    nearby_ids = list(range(start_id, end_id + 1))
    nearby_messages = await client.get_messages(source, ids=nearby_ids)
    album_messages = [
        nearby_message
        for nearby_message in nearby_messages
        if nearby_message is not None and nearby_message.grouped_id == message.grouped_id
    ]
    return sorted(album_messages, key=lambda item: item.id) or [message]


async def process_message_links(
    client,
    source,
    target,
    config: AppConfig,
    db: ProcessedDatabase,
    logger: logging.Logger,
    counters: Counters,
    progress: ProgressReporter,
) -> None:
    processed_source_ids: set[int] = set()
    message_ids = [parse_message_id(link) for link in config.message_links]
    logger.info("Processing specific message links ids=%s", message_ids)

    for message_id in message_ids:
        if config.send_limit is not None and counters.copied >= config.send_limit:
            break
        if message_id in processed_source_ids:
            logger.info("SKIP already handled linked source_id=%s in this run", message_id)
            continue

        message = await client.get_messages(source, ids=message_id)
        if message is None:
            counters.failed += 1
            logger.error("FAILED source_id=%s error=message not found", message_id)
            continue

        messages = await fetch_message_album(client, source, message)
        messages = [item for item in messages if item.id not in processed_source_ids]
        if not messages:
            continue

        for item in messages:
            if db.is_copied(config.source_channel, item.id):
                logger.info("FORCE link mode: recopying source_id=%s", item.id)

        counters.scanned += len(messages)
        processed_source_ids.update(item.id for item in messages)
        progress.counters(counters, "scanning")

        if len(messages) > 1 or messages[0].grouped_id:
            await process_album(
                client,
                target,
                messages,
                config,
                db,
                logger,
                counters,
                progress,
                force_recopy=True,
            )
        else:
            await process_single_message(
                client,
                target,
                messages[0],
                config,
                db,
                logger,
                counters,
                progress,
                force_recopy=True,
            )


async def run(
    config: AppConfig,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[Event] = None,
    log_handler: Optional[logging.Handler] = None,
) -> Counters:
    logger = setup_logging(
        config.log_file,
        extra_handlers=[log_handler] if log_handler is not None else None,
    )
    db = ProcessedDatabase(config.database_path)
    counters = Counters()
    pending_album: list[Message] = []
    pending_grouped_id: Optional[int] = None
    progress = ProgressReporter(progress_callback, cancel_event)
    stopped = False

    logger.info("Starting %s", "dry-run" if config.dry_run else "copy")
    if config.message_links:
        logger.info("Message-link mode enabled; skipping date-range history scan")
    else:
        logger.info("Date range UTC: %s to %s", config.start_date.isoformat(), config.end_date.isoformat())
    logger.info("Keywords=%s allowed_media=%s", config.keywords, sorted(config.allowed_media))

    try:
        try:
            progress.emit(status="scanning")
            async with create_client(config) as client:
                source = await client.get_entity(config.source_channel)
                target = await client.get_entity(config.target_channel)

                if config.message_links:
                    await process_message_links(client, source, target, config, db, logger, counters, progress)
                else:
                    async for message in iter_history_oldest_first(client, source, config):
                        progress.check_cancelled()
                        counters.scanned += 1
                        progress.counters(counters, "scanning")

                        if (
                            config.send_limit is not None
                            and counters.copied >= config.send_limit
                            and not pending_album
                        ):
                            break

                        if message.grouped_id:
                            if pending_album and message.grouped_id != pending_grouped_id:
                                await process_album(client, target, pending_album, config, db, logger, counters, progress)
                                pending_album = []
                            pending_grouped_id = message.grouped_id
                            pending_album.append(message)
                            continue

                        if pending_album:
                            await process_album(client, target, pending_album, config, db, logger, counters, progress)
                            pending_album = []
                            pending_grouped_id = None

                        await process_single_message(client, target, message, config, db, logger, counters, progress)

                    if pending_album:
                        await process_album(client, target, pending_album, config, db, logger, counters, progress)
        except asyncio.CancelledError:
            stopped = True
            logger.warning("STOP requested; cleaning up temporary downloads")
            cleanup_paths([config.temp_dir])

    finally:
        db.close()
        cleanup_empty_temp_dir(config.temp_dir)

    logger.info(
        "%s scanned=%s copied=%s skipped=%s failed=%s",
        "Stopped" if stopped else "Done",
        counters.scanned,
        counters.copied,
        counters.skipped,
        counters.failed,
    )
    if stopped:
        progress.cancel_event = None
    progress.counters(counters, "idle" if not stopped else "stopped")
    return counters


def cleanup_empty_temp_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        config = AppConfig.from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
        return
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
