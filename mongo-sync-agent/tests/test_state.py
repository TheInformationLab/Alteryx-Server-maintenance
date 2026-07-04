"""Tests for mongo_sync_agent.state: the SQLite-backed crash-safety store."""

from __future__ import annotations

from mongo_sync_agent.runner_types import RunSummary, UnitResult
from mongo_sync_agent.state import FileOffset, StateStore


def test_watermark_crud(tmp_state):
    tmp_state.set_watermark("mongo:db.coll", "objectid", "5f1e2d3c4b5a6f7e8d9c0b1a", "run-1")

    got = tmp_state.get_watermark("mongo:db.coll")

    assert got is not None
    assert got.kind == "objectid"
    assert got.value == "5f1e2d3c4b5a6f7e8d9c0b1a"
    assert got.run_id == "run-1"
    assert got.updated_at  # non-empty ISO timestamp string


def test_watermark_missing_returns_none(tmp_state):
    assert tmp_state.get_watermark("mongo:db.unknown") is None


def test_offsets_batch_commit(tmp_state):
    fo1 = FileOffset(
        fingerprint="fp-1",
        last_path="C:/logs/a.log",
        offset=100,
        file_size_at_read=1000,
        updated_at="",
    )
    fo2 = FileOffset(
        fingerprint="fp-2",
        last_path="C:/logs/b.log",
        offset=200,
        file_size_at_read=2000,
        updated_at="",
    )

    tmp_state.set_offsets("gallery", [fo1, fo2], "run-1")

    got = tmp_state.get_offsets("gallery")

    assert set(got.keys()) == {"fp-1", "fp-2"}
    assert got["fp-1"].last_path == "C:/logs/a.log"
    assert got["fp-1"].offset == 100
    assert got["fp-1"].file_size_at_read == 1000
    assert got["fp-2"].last_path == "C:/logs/b.log"
    assert got["fp-2"].offset == 200
    assert got["fp-2"].file_size_at_read == 2000


def test_reopen_after_close(tmp_path):
    db_path = tmp_path / "state.db"

    store = StateStore(db_path)
    store.set_watermark("mongo:db.coll", "timestamp", "2026-07-03T00:00:00.000Z", "run-1")
    store.close()

    reopened = StateStore(db_path)
    try:
        got = reopened.get_watermark("mongo:db.coll")
        assert got is not None
        assert got.value == "2026-07-03T00:00:00.000Z"
        assert got.kind == "timestamp"
    finally:
        reopened.close()


def test_record_run(tmp_state):
    units = [
        UnitResult(unit="AS_Queue", status="ok", docs=10, bytes_uploaded=1234),
        UnitResult(unit="AS_Jobs", status="failed", docs=0, bytes_uploaded=0, error="boom"),
    ]
    summary = RunSummary(
        run_id="run-1",
        run_dt="2026-07-03T00:00:00Z",
        duration_s=1.5,
        peak_rss_bytes=1_000_000,
        units=units,
    )

    # Should not raise, and should be idempotent (upsert) on a second call
    # with the same run_id.
    tmp_state.record_run(summary)
    tmp_state.record_run(summary)
