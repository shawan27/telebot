from __future__ import annotations

import asyncio
import logging
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
            entities = [message.entities or [] for message in chunk_messages]

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
                    formatting_entities=entities,
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
) -> None:
    if db.is_copied(config.source_channel, message.id):
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
) -> None:
    if not messages:
        return

    source_ids = [message.id for message in messages]
    if all(db.is_copied(config.source_channel, message_id) for message_id in source_ids):
        counters.skipped += len(source_ids)
        logger.info("SKIP already copied album source_ids=%s", source_ids)
        return

    selected, reason = selected_album_messages(messages, config)
    if not selected:
        counters.skipped += len(source_ids)
        if not config.dry_run:
            for message in messages:
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
        skipped_unselected = set(source_ids) - set(selected_ids)
        for message_id in skipped_unselected:
            db.mark_skipped(config.source_channel, message_id, "album item did not match allowed media")
        logger.info("COPIED album source_ids=%s target_ids=%s", selected_ids, target_ids)
        await asyncio.sleep(config.send_delay_seconds)
    except Exception as exc:
        counters.failed += len(selected_ids)
        for message_id in selected_ids:
            db.mark_failed(config.source_channel, message_id, str(exc))
        logger.exception("FAILED album source_ids=%s error=%s", selected_ids, exc)


async def run(config: AppConfig) -> Counters:
    logger = setup_logging(config.log_file)
    db = ProcessedDatabase(config.database_path)
    counters = Counters()
    pending_album: list[Message] = []
    pending_grouped_id: Optional[int] = None

    logger.info("Starting %s", "dry-run" if config.dry_run else "copy")
    logger.info("Date range UTC: %s to %s", config.start_date.isoformat(), config.end_date.isoformat())
    logger.info("Keywords=%s allowed_media=%s", config.keywords, sorted(config.allowed_media))

    try:
        async with create_client(config) as client:
            source = await client.get_entity(config.source_channel)
            target = await client.get_entity(config.target_channel)

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
