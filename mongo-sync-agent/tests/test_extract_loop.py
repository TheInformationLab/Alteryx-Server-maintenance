"""Tests for ``extract_collection`` -- the crash-safety and batching core.

These exercise the write -> upload -> commit ordering with the in-memory fakes
from ``conftest``:

* happy path advances the watermark only after a confirmed upload;
* an upload failure leaves the watermark untouched and cleans up the spool;
* zero docs short-circuits before any S3 call or watermark write;
* ``observe()`` is called exactly once per doc;
* ``write_rows`` is never handed more than ``batch_size`` rows.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from mongo_sync_agent import s3 as s3_module
from mongo_sync_agent.config import CollectionConfig
from mongo_sync_agent.mongo import extract as extract_mod
from mongo_sync_agent.mongo.extract import extract_collection
from mongo_sync_agent.mongo.watermark import make_strategy
from mongo_sync_agent.runner_types import RunContext
from mongo_sync_agent.s3 import S3Uploader
from mongo_sync_agent.sinks import ParquetVariantSink

from .conftest import FAKE_BUCKET, FakeCollection, FakeS3Client


def _run_ctx(spool_dir, dry_run: bool = False) -> RunContext:
    return RunContext(
        run_id="run-1",
        run_dt=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        spool_dir=spool_dir,
        dry_run=dry_run,
    )


def _cfg(**kw) -> CollectionConfig:
    kw.setdefault("mode", "append_only")
    kw.setdefault("name", "widgets")
    return CollectionConfig(**kw)


def _docs(n: int) -> list[dict]:
    return [{"_id": ObjectId(), "i": i} for i in range(n)]


class SpySink:
    """Wraps ParquetVariantSink, recording the size of every write_rows call."""

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


def _failing_uploader(tmp_path, monkeypatch) -> FakeS3Client:
    """Wire an S3Uploader whose boto3 client fails on its first upload."""
    fake_client = FakeS3Client.FailOnNthUpload(tmp_path, 1)
    monkeypatch.setattr(s3_module.boto3, "client", lambda *a, **k: fake_client)
    uploader = S3Uploader(bucket=FAKE_BUCKET, prefix="", region="us-east-1")
    return fake_client, uploader


def test_happy_path_advances_watermark(fake_s3, tmp_state, tmp_spool):
    fake_client, uploader = fake_s3
    docs = _docs(5)
    coll = FakeCollection(docs)

    result = extract_collection(
        coll, _cfg(), tmp_state, uploader, _run_ctx(tmp_spool), db_name="testdb"
    )

    assert result.status == "ok"
    assert result.docs == 5
    assert len(fake_client.upload_calls) == 1

    wm = tmp_state.get_watermark("mongo:testdb.widgets")
    assert wm is not None
    assert wm.kind == "objectid"
    assert wm.value == str(max(d["_id"] for d in docs))


def test_upload_failure_watermark_unchanged(tmp_path, tmp_state, tmp_spool, monkeypatch):
    _fake_client, uploader = _failing_uploader(tmp_path, monkeypatch)
    coll = FakeCollection(_docs(4))

    result = extract_collection(
        coll, _cfg(), tmp_state, uploader, _run_ctx(tmp_spool), db_name="testdb"
    )

    assert result.status == "failed"
    assert tmp_state.get_watermark("mongo:testdb.widgets") is None


def test_upload_failure_spool_cleaned(tmp_path, tmp_state, tmp_spool, monkeypatch):
    _fake_client, uploader = _failing_uploader(tmp_path, monkeypatch)
    run_ctx = _run_ctx(tmp_spool)
    coll = FakeCollection(_docs(4))

    extract_collection(coll, _cfg(), tmp_state, uploader, run_ctx, db_name="testdb")

    leftovers = list(run_ctx.spool_dir.iterdir())
    assert leftovers == [], f"spool not cleaned: {leftovers}"


def test_zero_docs_no_upload_no_watermark(fake_s3, tmp_state, tmp_spool):
    fake_client, uploader = fake_s3
    run_ctx = _run_ctx(tmp_spool)
    coll = FakeCollection([])

    result = extract_collection(
        coll, _cfg(), tmp_state, uploader, run_ctx, db_name="testdb"
    )

    assert result.status == "empty"
    assert fake_client.upload_calls == []
    assert tmp_state.get_watermark("mongo:testdb.widgets") is None
    assert list(run_ctx.spool_dir.iterdir()) == []


def test_observe_called_per_doc(fake_s3, tmp_state, tmp_spool, monkeypatch):
    _fake_client, uploader = fake_s3
    docs = _docs(7)
    coll = FakeCollection(docs)

    observed: list[dict] = []

    def spy_make(cfg):
        strat = make_strategy(cfg)
        original = strat.observe

        def wrapped(doc):
            observed.append(doc)
            return original(doc)

        strat.observe = wrapped  # type: ignore[method-assign]
        return strat

    monkeypatch.setattr(extract_mod, "make_strategy", spy_make)

    extract_collection(
        coll, _cfg(), tmp_state, uploader, _run_ctx(tmp_spool), db_name="testdb"
    )

    assert len(observed) == len(docs)


def test_batch_size_respected(fake_s3, tmp_state, tmp_spool, monkeypatch):
    _fake_client, uploader = fake_s3
    SpySink.instances.clear()
    monkeypatch.setattr(extract_mod, "ParquetVariantSink", SpySink)

    coll = FakeCollection(_docs(10))
    extract_collection(
        coll, _cfg(batch_size=3), tmp_state, uploader, _run_ctx(tmp_spool), db_name="testdb"
    )

    assert len(SpySink.instances) == 1
    sizes = SpySink.instances[0].call_sizes
    # 10 docs at batch_size=3 => flushes of [3, 3, 3, 1].
    assert len(sizes) >= 4
    assert max(sizes) <= 3
    assert sum(sizes) == 10
    SpySink.instances.clear()
