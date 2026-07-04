"""Streaming row sinks for the mongo-sync-agent extract loop.

A :class:`Sink` is the write end of the extract pipeline: the loop pulls docs
from a Mongo cursor in bounded batches, maps each doc to a row via a
:class:`~mongo_sync_agent.landing.LandingFormat`, and hands the resulting list
of row dicts to :meth:`Sink.write_rows`. When the collection is exhausted the
loop calls :meth:`Sink.close` (success) or :meth:`Sink.abort` (failure).

MEMORY INVARIANT
================
Peak extra RSS during extraction is ``O(batch_size x avg_doc_size)``,
independent of collection size. This holds because:

1. the Mongo cursor delivers docs in bounded driver batches;
2. the ``rows`` list in the caller is cleared after every ``write_rows`` call;
3. ``write_rows`` creates ONE Arrow ``RecordBatch`` (immediately written to the
   ``ParquetWriter`` as one row group, then GC'd);
4. ``ParquetWriter`` buffers only the current compressed page -- not the full
   file.

Nothing is ever accumulated in Python memory across batches. This module is the
second half of that invariant: :class:`ParquetVariantSink` flushes exactly one
row group per :meth:`write_rows` call and never retains a batch. Do NOT
accumulate batches in a list and write them together at close -- that would
break the invariant and defeat the entire point of streaming.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.parquet as pq

from .landing import LandingFormat


class SinkResult(NamedTuple):
    """Outcome of a completed sink: the total rows written and the file size."""

    rows: int
    bytes_written: int


@runtime_checkable
class Sink(Protocol):
    """Write end of the extract pipeline.

    A sink owns a single local spool file at ``path`` and a running ``rows``
    total. Rows arrive in batches via :meth:`write_rows`; the file is finalized
    by :meth:`close` (success) or discarded by :meth:`abort` (failure).
    """

    path: Path
    rows: int  # running total written so far

    def write_rows(self, rows: list[dict]) -> None:
        """Append a batch of row dicts. Must flush, never accumulate across calls."""
        ...

    def close(self) -> SinkResult:
        """Finalize the file and return the row count and byte size."""
        ...

    def abort(self) -> None:
        """Best-effort: try to close the writer, then unlink the file. Never raises."""
        ...


class ParquetVariantSink:
    """Streaming Parquet writer. ONE ROW GROUP PER ``write_rows()`` CALL.

    This is the second half of the memory invariant: each call builds a single
    Arrow ``RecordBatch``, writes it as its own row group, and drops it. The
    :class:`pyarrow.parquet.ParquetWriter` is created lazily on the first
    non-empty batch so that a collection which yields zero rows produces no
    file at all.

    Snappy compression is the default because it maximizes Snowflake
    ``COPY INTO`` compatibility (zstd works too, but snappy is safest).
    """

    def __init__(
        self, path: Path, landing: LandingFormat, compression: str = "snappy"
    ) -> None:
        self.path = path
        self.rows = 0
        self._landing = landing
        self._compression = compression
        self._writer: pq.ParquetWriter | None = None  # created lazily on first write

    def write_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        if self._writer is None:
            self._writer = pq.ParquetWriter(
                str(self.path), self._landing.schema, compression=self._compression
            )
        # Build ONE record batch from the row dicts (already keyed to the schema).
        batch = pa.RecordBatch.from_pylist(rows, schema=self._landing.schema)
        # write_table() on a single-batch Table flushes exactly one row group per
        # call. This is the crux of the memory invariant -- do NOT accumulate.
        self._writer.write_table(pa.Table.from_batches([batch]))
        self.rows += len(rows)

    def close(self) -> SinkResult:
        if self._writer is not None:
            self._writer.close()
        size = self.path.stat().st_size if self.path.exists() else 0
        return SinkResult(rows=self.rows, bytes_written=size)

    def abort(self) -> None:
        try:
            if self._writer is not None:
                self._writer.close()
        except Exception:
            pass
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


class GzipNdjsonSink:
    """Gzip-compressed NDJSON writer: one JSON object per line.

    Used for landing formats that are not naturally columnar (e.g. raw log or
    metric records). ``default=str`` on :func:`json.dumps` guarantees that any
    non-JSON-native value (datetimes, ObjectIds, etc.) is stringified rather
    than raising.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows = 0
        self._fh = gzip.open(path, "wt", encoding="utf-8")

    def write_rows(self, rows: list[dict]) -> None:
        for row in rows:
            self._fh.write(json.dumps(row, default=str) + "\n")
        self.rows += len(rows)

    def close(self) -> SinkResult:
        self._fh.close()
        size = self.path.stat().st_size if self.path.exists() else 0
        return SinkResult(rows=self.rows, bytes_written=size)

    def abort(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass
