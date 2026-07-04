"""Kill-matrix tests for the extract crash-safety ordering.

The load-bearing ordering in ``mongo/extract.py`` is:

    1 write spool -> 2 close sink (finalise) -> 3 upload to S3
    -> 4 commit watermark -> 5 delete spool

Each test kills the process at one boundary (via raise-once fault injection)
and asserts the at-least-once contract holds: the watermark is only advanced
after a confirmed upload, a crash before commit is safely retried (re-uploading
under a fresh S3 key), an orphaned spool from a post-commit crash is swept on
the next startup, and a clean re-run of unchanged data is idempotent (empty).

The fakes are ``FakeCollection`` and ``FakeS3Client`` from ``conftest``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bson import ObjectId

from mongo_sync_agent import s3 as s3_module
from mongo_sync_agent.config import CollectionConfig
from mongo_sync_agent.mongo.extract import extract_collection
from mongo_sync_agent.runner_types import RunContext
from mongo_sync_agent.s3 import S3Uploader
from mongo_sync_agent.spool import setup_spool, sweep_orphans
from mongo_sync_agent.state import StateStore

from .conftest import FAKE_BUCKET, FakeCollection, FakeS3Client

NAMESPACE = "mongo:testdb.widgets"


def _ctx(spool_dir: Path, second: int = 5, run_id: str = "run-1") -> RunContext:
    """A RunContext whose run_dt second varies so distinct runs get distinct keys."""
    return RunContext(
        run_id=run_id,
        run_dt=datetime(2024, 1, 2, 3, 4, second, tzinfo=timezone.utc),
        spool_dir=spool_dir,
        dry_run=False,
    )


def _cfg(**kw) -> CollectionConfig:
    kw.setdefault("name", "widgets")
    kw.setdefault("mode", "append_only")
    return CollectionConfig(**kw)


def _uploader(fake_client: FakeS3Client, monkeypatch) -> S3Uploader:
    monkeypatch.setattr(s3_module.boto3, "client", lambda *a, **k: fake_client)
    return S3Uploader(bucket=FAKE_BUCKET, prefix="", region="us-east-1")


def _recent_docs(n: int) -> list[dict]:
    return [{"_id": ObjectId(), "i": i} for i in range(n)]


def test_crash_during_upload_watermark_unchanged(tmp_path, tmp_state, tmp_spool, monkeypatch):
    """Kill during step 3: watermark unchanged after crash, next run succeeds."""
    # FakeS3Client raises on its FIRST upload, succeeds thereafter.
    fake = FakeS3Client.FailOnNthUpload(tmp_path / "s3", 1)
    uploader = _uploader(fake, monkeypatch)
    coll = FakeCollection(_recent_docs(5))
    cfg = _cfg()

    # First run: upload fails at step 3, so the watermark is never committed.
    r1 = extract_collection(coll, cfg, tmp_state, uploader, _ctx(tmp_spool, 5), "testdb")
    assert r1.status == "failed"
    assert tmp_state.get_watermark(NAMESPACE) is None
    assert list(tmp_spool.iterdir()) == []  # spool cleaned up on failure

    # Second run (same state, same docs): upload #2 succeeds -> watermark set.
    r2 = extract_collection(coll, cfg, tmp_state, uploader, _ctx(tmp_spool, 6), "testdb")
    assert r2.status == "ok"
    wm = tmp_state.get_watermark(NAMESPACE)
    assert wm is not None
    assert wm.value != ""


def test_crash_between_upload_and_commit(tmp_path, tmp_state, tmp_spool, monkeypatch):
    """Kill between step 3 and step 4 (at-least-once): re-uploads under a 2nd key.

    The upload succeeds but the watermark commit is interrupted. The next run
    re-extracts the same increment, uploads it under a fresh S3 key, and only
    then commits. Two S3 objects exist; the final watermark is set.
    """
    fake = FakeS3Client(tmp_path / "s3")  # uploads always succeed
    uploader = _uploader(fake, monkeypatch)
    coll = FakeCollection(_recent_docs(5))
    cfg = _cfg()

    # Inject a raise-once fault into the watermark commit (step 4).
    calls = {"n": 0}
    real_set = StateStore.set_watermark

    def flaky_set(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash between upload and commit")
        return real_set(self, *a, **k)

    monkeypatch.setattr(StateStore, "set_watermark", flaky_set)

    # First run: upload #1 succeeds, then the commit raises (propagates out).
    with pytest.raises(RuntimeError, match="crash between upload and commit"):
        extract_collection(coll, cfg, tmp_state, uploader, _ctx(tmp_spool, 5), "testdb")
    assert tmp_state.get_watermark(NAMESPACE) is None  # watermark NOT advanced
    assert len(fake.upload_calls) == 1

    # Second run: re-extracts (watermark still unset), uploads under a 2nd key,
    # then commits successfully.
    r2 = extract_collection(coll, cfg, tmp_state, uploader, _ctx(tmp_spool, 6), "testdb")
    assert r2.status == "ok"

    keys = {call[2] for call in fake.upload_calls}
    assert len(fake.upload_calls) == 2
    assert len(keys) == 2, f"expected two distinct S3 keys, got {keys}"
    wm = tmp_state.get_watermark(NAMESPACE)
    assert wm is not None and wm.value != ""


def test_crash_after_commit_before_spool_delete(tmp_path, tmp_state, monkeypatch):
    """Kill between step 4 and step 5: the next startup sweep removes the orphan."""
    spool_base = tmp_path / "spool"

    # Simulate a prior run that committed its watermark but died before deleting
    # its spool file: an orphaned per-run spool directory is left behind.
    orphan_dir = spool_base / "deadrun"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "widgets.parquet").write_bytes(b"orphaned spool payload")
    assert orphan_dir.exists()

    # Next startup: create the new run's spool dir, then sweep orphans.
    new_run = "freshrun"
    new_spool = setup_spool(str(spool_base), new_run)
    removed = sweep_orphans(str(spool_base), new_run)

    assert removed == 1
    assert not orphan_dir.exists()
    # The spool base now contains only the current run's directory.
    assert [p.name for p in spool_base.iterdir()] == [new_run]

    # And the run proceeds normally into the clean spool dir.
    fake = FakeS3Client(tmp_path / "s3")
    uploader = _uploader(fake, monkeypatch)
    coll = FakeCollection(_recent_docs(3))
    result = extract_collection(
        coll, _cfg(), tmp_state, uploader, _ctx(new_spool), "testdb"
    )

    assert result.status == "ok"
    # Its own spool file was deleted at step 5 -> the spool dir is clean.
    assert list(new_spool.iterdir()) == []


def test_idempotent_rerun(tmp_path, tmp_state, tmp_spool, monkeypatch):
    """A full run then an identical re-run: the second run is empty, watermark held.

    With ``overlap_seconds=0`` and docs whose ObjectIds carry the canonical
    watermark exactly (generated via ``from_datetime``), the second run's
    ``$gt`` filter excludes every already-shipped doc, so no new work is found.
    """
    fake = FakeS3Client(tmp_path / "s3")
    uploader = _uploader(fake, monkeypatch)

    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    docs = [{"_id": ObjectId.from_datetime(base + timedelta(seconds=i)), "i": i} for i in range(5)]
    coll = FakeCollection(docs)
    cfg = _cfg(overlap_seconds=0)

    r1 = extract_collection(coll, cfg, tmp_state, uploader, _ctx(tmp_spool, 5), "testdb")
    assert r1.status == "ok"
    assert r1.docs == 5
    wm1 = tmp_state.get_watermark(NAMESPACE)
    assert wm1 is not None

    r2 = extract_collection(coll, cfg, tmp_state, uploader, _ctx(tmp_spool, 6), "testdb")
    assert r2.status == "empty"

    wm2 = tmp_state.get_watermark(NAMESPACE)
    assert wm2 is not None
    assert wm2.value == wm1.value  # watermark unchanged
    assert len(fake.upload_calls) == 1  # nothing new uploaded on the empty rerun
