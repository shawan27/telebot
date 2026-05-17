from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


class ProcessedDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                source_channel TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                grouped_id TEXT,
                status TEXT NOT NULL,
                target_message_ids TEXT,
                error TEXT,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (source_channel, source_message_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_processed_status
            ON processed_messages (source_channel, status)
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def is_copied(self, source_channel: str, message_id: int) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM processed_messages
            WHERE source_channel = ? AND source_message_id = ? AND status = 'copied'
            """,
            (source_channel, message_id),
        ).fetchone()
        return row is not None

    def copied_count(self, source_channel: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count FROM processed_messages
            WHERE source_channel = ? AND status = 'copied'
            """,
            (source_channel,),
        ).fetchone()
        return int(row["count"])

    def mark_copied(
        self,
        source_channel: str,
        source_message_ids: Iterable[int],
        target_message_ids: Iterable[int],
        grouped_id: Optional[int] = None,
    ) -> None:
        target_json = json.dumps(list(target_message_ids))
        now = _utc_now()
        with self.conn:
            for message_id in source_message_ids:
                self.conn.execute(
                    """
                    INSERT INTO processed_messages (
                        source_channel, source_message_id, grouped_id, status,
                        target_message_ids, error, processed_at
                    )
                    VALUES (?, ?, ?, 'copied', ?, NULL, ?)
                    ON CONFLICT(source_channel, source_message_id)
                    DO UPDATE SET
                        grouped_id = excluded.grouped_id,
                        status = 'copied',
                        target_message_ids = excluded.target_message_ids,
                        error = NULL,
                        processed_at = excluded.processed_at
                    """,
                    (source_channel, message_id, _grouped_id(grouped_id), target_json, now),
                )

    def mark_skipped(self, source_channel: str, message_id: int, reason: str) -> None:
        self._mark_status(source_channel, message_id, "skipped", reason)

    def mark_failed(self, source_channel: str, message_id: int, error: str) -> None:
        self._mark_status(source_channel, message_id, "failed", error)

    def _mark_status(self, source_channel: str, message_id: int, status: str, error: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO processed_messages (
                    source_channel, source_message_id, grouped_id, status,
                    target_message_ids, error, processed_at
                )
                VALUES (?, ?, NULL, ?, NULL, ?, ?)
                ON CONFLICT(source_channel, source_message_id)
                DO UPDATE SET
                    status = excluded.status,
                    error = excluded.error,
                    processed_at = excluded.processed_at
                """,
                (source_channel, message_id, status, error, _utc_now()),
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _grouped_id(value: Optional[int]) -> Optional[str]:
    return str(value) if value is not None else None
