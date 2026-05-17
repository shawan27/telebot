from __future__ import annotations

from collections.abc import AsyncIterator

from telethon import TelegramClient
from telethon.tl.custom.message import Message


def create_client(config) -> TelegramClient:
    return TelegramClient(config.session_name, config.api_id, config.api_hash)


async def iter_history_oldest_first(client: TelegramClient, source, config) -> AsyncIterator[Message]:
    async for message in client.iter_messages(
        source,
        reverse=True,
        offset_date=config.start_date,
        limit=config.scan_limit,
        wait_time=config.history_wait_seconds,
    ):
        if message.date is None:
            continue
        message_date = message.date.astimezone(config.start_date.tzinfo)
        if message_date < config.start_date:
            continue
        if message_date > config.end_date:
            break
        yield message
