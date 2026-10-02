"""Resumable ingestion ledger.

CLAUDE.md §5: re-running ingestion must not duplicate records, and a failure at
document 4,000 of 6,000 must not restart from zero.

Implementation is a SQLite file, which is stdlib, single-file, crash-safe, and
inspectable with any sqlite3 client years from now. A JSONL append log would also
work but makes "have I already done this key?" an O(n) scan and makes retry
bookkeeping awkward.

The ledger is derived state, not the archive. Delete it and a full re-run rebuilds
it. It lives in data/ and is gitignored.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .records import utc_now

DEFAULT_LEDGER = Path("data/ingest-ledger.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested (
    s3_key        TEXT PRIMARY KEY,
    record_id     TEXT,
    text_sha256   TEXT,
    etag          TEXT,
    byte_size     INTEGER,
    status        TEXT NOT NULL,          -- done | failed | skipped
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingested_status ON ingested(status);
CREATE INDEX IF NOT EXISTS idx_ingested_text_sha ON ingested(text_sha256);
"""


@dataclass
class LedgerEntry:
    s3_key: str
    status: str
    record_id: str | None = None
    text_sha256: str | None = None
    etag: str | None = None
    byte_size: int | None = None
    attempts: int = 0
    error: str | None = None
    updated_at: str | None = None


class Ledger:
    """Tracks which objects have been processed, and whether their bytes changed.

    Typical use:

        with Ledger() as ledger:
            for obj in objects:
                if ledger.is_done(obj["Key"], etag=obj["ETag"]):
                    continue
                try:
                    record = ingest(obj)
                except Exception as exc:
                    ledger.mark_failed(obj["Key"], str(exc))
                    continue
                ledger.mark_done(record, etag=obj["ETag"], byte_size=obj["Size"])
    """

    def __init__(self, path: str | Path = DEFAULT_LEDGER) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")   # survives an abrupt kill
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- queries ---------------------------------------------------------------

    def get(self, s3_key: str) -> LedgerEntry | None:
        row = self.conn.execute(
            "SELECT * FROM ingested WHERE s3_key = ?", (s3_key,)
        ).fetchone()
        return LedgerEntry(**dict(row)) if row else None

    def is_done(self, s3_key: str, etag: str | None = None) -> bool:
        """True when this key succeeded and, if an etag is given, the bytes match.

        Passing the etag is what makes re-ingestion pick up a changed object
        instead of skipping it — important if the S3 archive is ever re-crawled.
        """
        entry = self.get(s3_key)
        if entry is None or entry.status != "done":
            return False
        if etag is None or entry.etag is None:
            return True
        return entry.etag.strip('"') == etag.strip('"')

    def seen_text(self, text_sha256: str) -> str | None:
        """Record ID of the first document with this text, if any.

        Deduplication is on content, not filename (CLAUDE.md §5) — the same PDF is
        likely archived under several names across sites.
        """
        row = self.conn.execute(
            "SELECT record_id FROM ingested WHERE text_sha256 = ? AND status = 'done' "
            "ORDER BY updated_at LIMIT 1",
            (text_sha256,),
        ).fetchone()
        return row["record_id"] if row else None

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM ingested GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def failures(self, limit: int = 100) -> list[LedgerEntry]:
        rows = self.conn.execute(
            "SELECT * FROM ingested WHERE status = 'failed' "
            "ORDER BY attempts DESC, updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [LedgerEntry(**dict(row)) for row in rows]

    # -- writes ----------------------------------------------------------------

    def _upsert(self, entry: LedgerEntry) -> None:
        self.conn.execute(
            """
            INSERT INTO ingested
                (s3_key, record_id, text_sha256, etag, byte_size, status, attempts,
                 error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(s3_key) DO UPDATE SET
                record_id   = excluded.record_id,
                text_sha256 = excluded.text_sha256,
                etag        = excluded.etag,
                byte_size   = excluded.byte_size,
                status      = excluded.status,
                attempts    = ingested.attempts + 1,
                error       = excluded.error,
                updated_at  = excluded.updated_at
            """,
            (
                entry.s3_key, entry.record_id, entry.text_sha256, entry.etag,
                entry.byte_size, entry.status, 1, entry.error, utc_now(),
            ),
        )
        self.conn.commit()

    def mark_done(
        self,
        s3_key: str,
        record_id: str,
        text_sha256: str,
        etag: str | None = None,
        byte_size: int | None = None,
    ) -> None:
        self._upsert(LedgerEntry(
            s3_key=s3_key, status="done", record_id=record_id,
            text_sha256=text_sha256, etag=etag, byte_size=byte_size,
        ))

    def mark_failed(self, s3_key: str, error: str) -> None:
        self._upsert(LedgerEntry(s3_key=s3_key, status="failed", error=error[:2000]))

    def mark_skipped(self, s3_key: str, reason: str) -> None:
        """For objects we deliberately do not ingest — thumbnails, nav gifs, etc."""
        self._upsert(LedgerEntry(s3_key=s3_key, status="skipped", error=reason[:2000]))
