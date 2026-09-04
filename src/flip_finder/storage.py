from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Listing


class SeenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                listing_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                url TEXT NOT NULL,
                first_seen TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                listing_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                analyzed_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS retry_queue (
                listing_id TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def is_seen(self, listing_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM seen WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, listing: Listing) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO seen(listing_id, source, url, first_seen)
            VALUES (?, ?, ?, ?)
            """,
            (listing.listing_id, listing.source, listing.url, listing.collected_at),
        )
        self.connection.execute(
            "DELETE FROM retry_queue WHERE listing_id = ?", (listing.listing_id,)
        )
        self.connection.commit()

    def save_analysis(self, listing_id: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO analyses(listing_id, payload, analyzed_at)
            VALUES (?, ?, ?)
            """,
            (
                listing_id,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()

    def schedule_retry(self, listing_id: str, reason: str) -> None:
        row = self.connection.execute(
            "SELECT attempts FROM retry_queue WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        attempts = int(row[0]) + 1 if row else 1
        delay = min(21_600, 900 * (2 ** min(attempts - 1, 4)))
        retry_at = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT OR REPLACE INTO retry_queue(listing_id, attempts, next_retry_at, reason)
            VALUES (?, ?, ?, ?)
            """,
            (listing_id, attempts, retry_at, reason[:300]),
        )
        self.connection.commit()

    def pending_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM retry_queue").fetchone()
        return int(row[0]) if row else 0

