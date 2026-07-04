"""SQLite-backed state store.

This module is the crash-safety commit point for the entire agent. All
watermark, file-offset, and run-summary bookkeeping is written through here
using durable, serialized transactions so that a crash or power loss can
never leave the on-disk state half-written.

Design notes:
    - The connection is opened with ``isolation_level=None`` (autocommit at
      the driver level) so that every mutation can use an explicit
      ``BEGIN IMMEDIATE ... COMMIT`` block. ``BEGIN IMMEDIATE`` acquires the
      write lock up front, which avoids the classic sqlite3 "deferred
      transaction" upgrade race.
    - ``journal_mode=WAL`` and ``synchronous=FULL`` are set so that a commit
      is not reported as successful until it is durable on disk.
    - ``busy_timeout=5000`` makes concurrent access from another process (or
      thread) retry for up to 5 seconds instead of raising immediately.
    - Watermark values are stored and returned as plain strings. This module
      does not know or care what "kind" means beyond an opaque tag; callers
      are responsible for comparing the returned ``kind`` against what they
      expect and deciding how to react to a mismatch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


class Watermark(NamedTuple):
    """A single collection's replication watermark."""

    kind: str
    value: str
    updated_at: str
    run_id: str


class FileOffset(NamedTuple):
    """Byte offset bookkeeping for a single tailed log file."""

    fingerprint: str
    last_path: str
    offset: int
    file_size_at_read: int
    updated_at: str


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Durable SQLite-backed store for watermarks, file offsets, and run history.

    This is the crash-safety commit point for the agent: every mutation is
    wrapped in an explicit ``BEGIN IMMEDIATE ... COMMIT`` transaction against
    a WAL-mode, fully-synchronous SQLite database, so a partially-applied
    write can never be observed after a crash.
    """

    def __init__(self, path: str | Path):
        """Open (creating if necessary) the state database at ``path``.

        Args:
            path: Filesystem path to the SQLite database file. Parent
                directories are created if they do not exist.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None puts the connection in autocommit mode at the
        # sqlite3 driver level, so we control transaction boundaries
        # explicitly via BEGIN IMMEDIATE / COMMIT statements below.
        self._conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the required tables if they do not already exist."""
        with self._transaction() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watermarks (
                    namespace TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    run_id TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS file_offsets (
                    source TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    last_path TEXT NOT NULL,
                    offset INTEGER NOT NULL,
                    file_size_at_read INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    PRIMARY KEY (source, fingerprint)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    run_dt TEXT NOT NULL,
                    duration_s REAL,
                    peak_rss_bytes INTEGER,
                    units_json TEXT,
                    recorded_at TEXT NOT NULL
                )
                """
            )

    def _transaction(self):
        """Return a context manager that runs a ``BEGIN IMMEDIATE ... COMMIT`` block.

        On any exception the transaction is rolled back and the exception is
        re-raised.
        """
        return _ImmediateTransaction(self._conn)

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    # -- Watermarks ----------------------------------------------------

    def get_watermark(self, namespace: str) -> Watermark | None:
        """Fetch the current watermark for ``namespace``, if one exists.

        Args:
            namespace: Opaque identifier for the thing being watermarked
                (e.g. ``"mongo:<db>:<collection>"``).

        Returns:
            The stored :class:`Watermark`, or ``None`` if no watermark has
            ever been recorded for this namespace.
        """
        cur = self._conn.execute(
            "SELECT kind, value, updated_at, run_id FROM watermarks WHERE namespace = ?",
            (namespace,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return Watermark(kind=row[0], value=row[1], updated_at=row[2], run_id=row[3])

    def set_watermark(self, namespace: str, kind: str, value: str, run_id: str) -> None:
        """Upsert the watermark for ``namespace``.

        Args:
            namespace: Opaque identifier for the thing being watermarked.
            kind: Opaque tag describing the shape of ``value`` (e.g.
                ``"objectid"``, ``"timestamp"``, ``"int"``). Stored and
                returned verbatim; this store does not interpret it.
            value: Canonical string form of the watermark. Callers are
                responsible for encoding/decoding whatever native type this
                represents; only strings are stored here.
            run_id: Identifier of the run that produced this watermark.
        """
        updated_at = _utcnow_iso()
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO watermarks (namespace, kind, value, updated_at, run_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(namespace) DO UPDATE SET
                    kind = excluded.kind,
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    run_id = excluded.run_id
                """,
                (namespace, kind, value, updated_at, run_id),
            )

    # -- File offsets ----------------------------------------------------

    def get_offsets(self, source: str) -> dict[str, FileOffset]:
        """Fetch all known file offsets for ``source``, keyed by fingerprint.

        Args:
            source: Identifier of the log source (typically a
                ``LogSourceConfig.name``).

        Returns:
            A dict mapping fingerprint to :class:`FileOffset`. Empty if no
            offsets have been recorded for this source.
        """
        cur = self._conn.execute(
            """
            SELECT fingerprint, last_path, offset, file_size_at_read, updated_at
            FROM file_offsets WHERE source = ?
            """,
            (source,),
        )
        result: dict[str, FileOffset] = {}
        for fingerprint, last_path, offset, file_size_at_read, updated_at in cur.fetchall():
            result[fingerprint] = FileOffset(
                fingerprint=fingerprint,
                last_path=last_path,
                offset=offset,
                file_size_at_read=file_size_at_read,
                updated_at=updated_at,
            )
        return result

    def set_offsets(self, source: str, offsets: list[FileOffset], run_id: str) -> None:
        """Upsert a batch of file offsets for ``source`` in a single transaction.

        Args:
            source: Identifier of the log source.
            offsets: The offsets to persist. All are committed atomically:
                either every row is written, or (on error) none are.
            run_id: Identifier of the run that produced these offsets.
        """
        updated_at = _utcnow_iso()
        with self._transaction() as cur:
            cur.executemany(
                """
                INSERT INTO file_offsets
                    (source, fingerprint, last_path, offset, file_size_at_read, updated_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, fingerprint) DO UPDATE SET
                    last_path = excluded.last_path,
                    offset = excluded.offset,
                    file_size_at_read = excluded.file_size_at_read,
                    updated_at = excluded.updated_at,
                    run_id = excluded.run_id
                """,
                [
                    (
                        source,
                        fo.fingerprint,
                        fo.last_path,
                        fo.offset,
                        fo.file_size_at_read,
                        updated_at,
                        run_id,
                    )
                    for fo in offsets
                ],
            )

    def delete_offset(self, source: str, fingerprint: str) -> None:
        """Delete a single file offset row.

        Args:
            source: Identifier of the log source.
            fingerprint: Fingerprint of the file whose offset should be
                forgotten (e.g. because it was rotated away and fully
                consumed).
        """
        with self._transaction() as cur:
            cur.execute(
                "DELETE FROM file_offsets WHERE source = ? AND fingerprint = ?",
                (source, fingerprint),
            )

    # -- Run history ----------------------------------------------------

    def record_run(self, summary) -> None:
        """Persist a run summary.

        Args:
            summary: A ``RunSummary``-shaped object with attributes
                ``run_id``, ``run_dt``, ``duration_s``, ``peak_rss_bytes``,
                and ``units`` (a list of ``UnitResult``-shaped objects with
                ``unit``, ``status``, ``docs``, ``bytes_uploaded``, and
                ``error`` attributes). ``units`` is serialised to JSON for
                storage.
        """
        units_json = json.dumps(
            [
                {
                    "unit": u.unit,
                    "status": u.status,
                    "docs": u.docs,
                    "bytes_uploaded": u.bytes_uploaded,
                    "error": u.error,
                }
                for u in summary.units
            ]
        )
        recorded_at = _utcnow_iso()
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO runs (run_id, run_dt, duration_s, peak_rss_bytes, units_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    run_dt = excluded.run_dt,
                    duration_s = excluded.duration_s,
                    peak_rss_bytes = excluded.peak_rss_bytes,
                    units_json = excluded.units_json,
                    recorded_at = excluded.recorded_at
                """,
                (
                    summary.run_id,
                    summary.run_dt,
                    summary.duration_s,
                    summary.peak_rss_bytes,
                    units_json,
                    recorded_at,
                ),
            )


class _ImmediateTransaction:
    """Context manager wrapping a single ``BEGIN IMMEDIATE ... COMMIT`` block.

    ``BEGIN IMMEDIATE`` acquires the SQLite write lock immediately rather
    than lazily on the first write statement, which avoids the "database is
    locked" upgrade race that can occur with plain ``BEGIN`` under
    concurrent access. On exception the transaction is rolled back so the
    database is never left with a partially-applied mutation.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> sqlite3.Cursor:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn.cursor()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
