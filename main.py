from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telethon.tl.custom.message import Message

from config import AppConfig, build_arg_parser
from database import ProcessedDatabase
from filters import selected_album_messages, should_copy_message
from telegram_client import create_client, iter_history_oldest_first
from utils import cleanup_paths, ensure_temp_dir, normalize_sent_ids, setup_logging, with_retries


@dataclass
class Counters:
    scanned: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0


MESSAGE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/\d+/|[^/]+/)(\d+)(?:[/?#].*)?$")


async def download_message_media(client, message: Message, config: AppConfig, logger: logging.Logger) -> str:
    ensure_temp_dir(config.temp_dir)

    async def _download() -> str:
        path = await client.download_media(message, file=str(config.temp_dir))
        if not path:
            raise RuntimeError(f"download returned no path for source message {message.id}")
        return str(path)

    return await with_retries(
        f"download source message {message.id}",
        _download,
        attempts=config.retry_attempts,
        logger=logger,
    )


async def send_text(client, target, message: Message, config: AppConfig, logger: logging.Logger) -> list[int]:
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
    )
    return normalize_sent_ids(sent)


async def send_media(client, target, message: Message, config: AppConfig, logger: logging.Logger) -> list[int]:
    paths: list[str] = []
    try:
        media_path = await download_message_media(client, message, config, logger)
        paths.append(media_path)

        async def _send():
            return await client.send_file(
                target,
                media_path,
                caption=message.message or None,
                formatting_entities=message.entities,
                force_document=not bool(message.video or message.photo),
                supports_streaming=bool(message.video),
            )

        sent = await with_retries(
            f"upload source message {message.id}",
            _send,
            attempts=config.retry_attempts,
            logger=logger,
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
) -> list[int]:
    sent_ids: list[int] = []
    paths: list[str] = []
    try:
        for message in messages:
            paths.append(await download_message_media(client, message, config, logger))

        for index in range(0, len(paths), 10):
            chunk_paths = paths[index : index + 10]
            chunk_messages = messages[index : index + 10]
            captions = [message.message or "" for message in chunk_messages]

            async def _send_chunk():
                if len(chunk_paths) == 1:
                    only_message = chunk_messages[0]
                    return await client.send_file(
                        target,
                        chunk_paths[0],
                        caption=only_message.message or None,
                        formatting_entities=only_message.entities,
                        force_document=not bool(only_message.video or only_message.photo),
                        supports_streaming=bool(only_message.video),
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
                )

            sent = await with_retries(
                "upload album " + ",".join(str(message.id) for message in chunk_messages),
                _send_chunk,
                attempts=config.retry_attempts,
                logger=logger,
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
    force_recopy: bool = False,
) -> None:
    if db.is_copied(config.source_channel, message.id) and not force_recopy:
        counters.skipped += 1
        logger.info("SKIP already copied source_id=%s", message.id)
        return

    result = should_copy_message(message, config)
    if not result.should_copy:
        counters.skipped += 1
        if not config.dry_run:
            db.mark_skipped(config.source_channel, message.id, result.reason)
        logger.info("SKIP source_id=%s reason=%s", message.id, result.reason)
        return

    if config.send_limit is not None and counters.copied >= config.send_limit:
        return

    if config.dry_run:
        counters.copied += 1
        logger.info("DRY-RUN copy source_id=%s reason=%s", message.id, result.reason)
        return

    try:
        if message.media:
            target_ids = await send_media(client, target, message, config, logger)
        else:
            target_ids = await send_text(client, target, message, config, logger)
        db.mark_copied(config.source_channel, [message.id], target_ids)
        counters.copied += 1
        logger.info("COPIED source_id=%s target_ids=%s", message.id, target_ids)
        await asyncio.sleep(config.send_delay_seconds)
    except Exception as exc:
        counters.failed += 1
        db.mark_failed(config.source_channel, message.id, str(exc))
        logger.exception("FAILED source_id=%s error=%s", message.id, exc)


async def process_album(
    client,
    target,
    messages: list[Message],
    config: AppConfig,
    db: ProcessedDatabase,
    logger: logging.Logger,
    counters: Counters,
    force_recopy: bool = False,
) -> None:
    if not messages:
        return

    source_ids = [message.id for message in messages]
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
            return

        uncopied_messages = [message for message in messages if message.id not in copied_ids]
        if copied_ids:
            counters.skipped += len(copied_ids)
            logger.info("SKIP already copied album items source_ids=%s", sorted(copied_ids))

    selected, reason = selected_album_messages(uncopied_messages, config)
    if not selected:
        counters.skipped += len(uncopied_messages)
        if not config.dry_run:
            for message in uncopied_messages:
                db.mark_skipped(config.source_channel, message.id, reason)
        logger.info("SKIP album source_ids=%s reason=%s", source_ids, reason)
        return

    selected_ids = [message.id for message in selected]
    if config.send_limit is not None and counters.copied + len(selected_ids) > config.send_limit:
        logger.info("STOP send limit would split album source_ids=%s", selected_ids)
        return

    if config.dry_run:
        counters.copied += len(selected_ids)
        logger.info("DRY-RUN copy album source_ids=%s reason=%s", selected_ids, reason)
        return

    try:
        target_ids = await send_album(client, target, selected, config, logger)
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
        await asyncio.sleep(config.send_delay_seconds)
    except Exception as exc:
        counters.failed += len(selected_ids)
        for message_id in selected_ids:
            db.mark_failed(config.source_channel, message_id, str(exc))
        logger.exception("FAILED album source_ids=%s error=%s", selected_ids, exc)


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

        if len(messages) > 1 or messages[0].grouped_id:
            await process_album(
                client,
                target,
                messages,
                config,
                db,
                logger,
                counters,
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
                force_recopy=True,
            )


async def run(config: AppConfig) -> Counters:
    logger = setup_logging(config.log_file)
    db = ProcessedDatabase(config.database_path)
    counters = Counters()
    pending_album: list[Message] = []
    pending_grouped_id: Optional[int] = None

    logger.info("Starting %s", "dry-run" if config.dry_run else "copy")
    if config.message_links:
        logger.info("Message-link mode enabled; skipping date-range history scan")
    else:
        logger.info("Date range UTC: %s to %s", config.start_date.isoformat(), config.end_date.isoformat())
    logger.info("Keywords=%s allowed_media=%s", config.keywords, sorted(config.allowed_media))

    try:
        async with create_client(config) as client:
            source = await client.get_entity(config.source_channel)
            target = await client.get_entity(config.target_channel)

            if config.message_links:
                await process_message_links(client, source, target, config, db, logger, counters)
            else:
                async for message in iter_history_oldest_first(client, source, config):
                    counters.scanned += 1

                    if (
                        config.send_limit is not None
                        and counters.copied >= config.send_limit
                        and not pending_album
                    ):
                        break

                    if message.grouped_id:
                        if pending_album and message.grouped_id != pending_grouped_id:
                            await process_album(client, target, pending_album, config, db, logger, counters)
                            pending_album = []
                        pending_grouped_id = message.grouped_id
                        pending_album.append(message)
                        continue

                    if pending_album:
                        await process_album(client, target, pending_album, config, db, logger, counters)
                        pending_album = []
                        pending_grouped_id = None

                    await process_single_message(client, target, message, config, db, logger, counters)

                if pending_album:
                    await process_album(client, target, pending_album, config, db, logger, counters)

    finally:
        db.close()
        cleanup_empty_temp_dir(config.temp_dir)

    logger.info(
        "Done scanned=%s copied=%s skipped=%s failed=%s",
        counters.scanned,
        counters.copied,
        counters.skipped,
        counters.failed,
    )
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
