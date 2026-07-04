"""The memory invariant: peak extra RSS is O(batch_size x avg_doc_size).

Two layers:

* Layer 1 (structural, always runs): a spy sink proves ``write_rows`` is never
  handed more than ``batch_size`` rows, so nothing accumulates across batches.
* Layer 2 (empirical, ``-m slow``): stream ~300 MB of logical data through
  ``extract_collection`` from a lazy generator and assert RSS stays bounded.

Layer 2 uses a bespoke generator-backed collection rather than ``conftest``'s
``FakeCollection`` (which materialises its docs) precisely because materialising
the dataset would itself violate the invariant under test.
"""

from __future__ import annotations

import gc
import os
from datetime import datetime, timezone

import psutil
import pytest
from bson import ObjectId

from mongo_sync_agent import s3 as s3_module
from mongo_sync_agent.config import CollectionConfig
from mongo_sync_agent.mongo import extract as extract_mod
from mongo_sync_agent.mongo.extract import extract_collection
from mongo_sync_agent.runner_types import RunContext
from mongo_sync_agent.s3 import S3Uploader
from mongo_sync_agent.sinks import ParquetVariantSink

from .conftest import FAKE_BUCKET, FakeCollection, FakeS3Client

_PROC = psutil.Process(os.getpid())


def _run_ctx(spool_dir) -> RunContext:
    return RunContext(
        run_id="mem-run",
        run_dt=datetime(2024, 1, 1, tzinfo=timezone.utc),
        spool_dir=spool_dir,
        dry_run=False,
    )


# --------------------------------------------------------------------------- #
# Layer 1 -- structural
# --------------------------------------------------------------------------- #

class SpySink:
    """Wraps ParquetVariantSink and records each write_rows batch size."""

    instances: list["SpySink"] = []

    def __init__(self, path, landing, compression: str = "snappy"):
        self._inner = ParquetVariantSink(path, landing, compression)
        self.call_sizes: list[int] = []
        SpySink.instances.append(self)

    @property
    def path(self):
        return self._inner.path

    @property
    def rows(self) -> int:
        return self._inner.rows

    def write_rows(self, rows: list[dict]) -> None:
        self.call_sizes.append(len(rows))
        self._inner.write_rows(rows)

    def close(self):
        return self._inner.close()

    def abort(self) -> None:
        self._inner.abort()


def test_spy_sink_never_exceeds_batch_size(fake_s3, tmp_state, tmp_spool, monkeypatch):
    """write_rows is never called with more than batch_size rows."""
    _fake_client, uploader = fake_s3
    SpySink.instances.clear()
    monkeypatch.setattr(extract_mod, "ParquetVariantSink", SpySink)

    docs = [{"_id": ObjectId(), "n": i} for i in range(500)]
    coll = FakeCollection(docs)
    cfg = CollectionConfig(name="c", mode="append_only", batch_size=50)

    extract_collection(coll, cfg, tmp_state, uploader, _run_ctx(tmp_spool), db_name="db")

    assert len(SpySink.instances) == 1
    call_sizes = SpySink.instances[0].call_sizes
    assert call_sizes, "sink was never written to"
    assert max(call_sizes) <= 50
    assert sum(call_sizes) == 500
    SpySink.instances.clear()


# --------------------------------------------------------------------------- #
# Layer 2 -- empirical (slow)
# --------------------------------------------------------------------------- #

class StreamingCollection:
    """Generator-backed collection: never materialises its docs into a list.

    ``find()`` returns a fresh generator (which has a ``.close()`` method, so it
    satisfies the extract loop's cursor contract) producing ``n`` docs of
    roughly ``size`` payload bytes each, one at a time.
    """

    def __init__(self, n: int, size: int):
        self._n = n
        self._size = size

    def find(self, filter=None, sort=None, batch_size=1000, no_cursor_timeout=False):
        n, size = self._n, self._size

        def gen():
            reps = size // 5
            for i in range(n):
                # Fresh ~size-byte string per doc so nothing is shared/interned.
                yield {"_id": ObjectId(), "blob": ("%05d" % (i % 100000)) * reps, "n": i}

        return gen()


class RssSamplingSink:
    """Delegates to ParquetVariantSink, sampling RSS after each flush."""

    peak = 0

    def __init__(self, path, landing, compression: str = "snappy"):
        self._inner = ParquetVariantSink(path, landing, compression)

    @property
    def path(self):
        return self._inner.path

    @property
    def rows(self) -> int:
        return self._inner.rows

    def write_rows(self, rows: list[dict]) -> None:
        self._inner.write_rows(rows)
        RssSamplingSink.peak = max(RssSamplingSink.peak, _PROC.memory_info().rss)

    def close(self):
        return self._inner.close()

    def abort(self) -> None:
        self._inner.abort()


@pytest.mark.slow
def test_rss_bounded_during_large_extraction(tmp_path, tmp_state, tmp_spool, monkeypatch):
    """RSS delta stays below 100 MB while streaming ~300 MB of logical data."""
    # Wire a real S3Uploader onto a filesystem-backed fake client.
    fake_client = FakeS3Client(tmp_path)
    monkeypatch.setattr(s3_module.boto3, "client", lambda *a, **k: fake_client)
    uploader = S3Uploader(bucket=FAKE_BUCKET, prefix="", region="us-east-1")

    # Sample intra-run peak RSS at every flush.
    monkeypatch.setattr(extract_mod, "ParquetVariantSink", RssSamplingSink)

    n_docs, doc_size = 60_000, 5_000  # ~300 MB of logical payload
    coll = StreamingCollection(n_docs, doc_size)
    cfg = CollectionConfig(name="big", mode="full_refresh", batch_size=500)

    gc.collect()
    baseline_rss = _PROC.memory_info().rss
    RssSamplingSink.peak = baseline_rss

    result = extract_collection(
        coll, cfg, tmp_state, uploader, _run_ctx(tmp_spool), db_name="db"
    )

    assert result.status == "ok"
    assert result.docs == n_docs

    peak_rss = max(RssSamplingSink.peak, _PROC.memory_info().rss)
    delta_mb = (peak_rss - baseline_rss) / 1_048_576
    assert delta_mb < 100, f"RSS grew by {delta_mb:.1f} MB -- memory invariant violated"
