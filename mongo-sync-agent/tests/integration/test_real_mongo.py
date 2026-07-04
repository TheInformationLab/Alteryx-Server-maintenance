"""Integration tests against a real MongoDB instance.

Skipped unless MSA_TEST_MONGO_URI is set. Point it at a disposable/test-only
database — these tests create and drop real collections.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

MONGO_URI = os.environ.get("MSA_TEST_MONGO_URI")
pytestmark = pytest.mark.skipif(not MONGO_URI, reason="Set MSA_TEST_MONGO_URI to run")


def test_ping_and_list_collections(tmp_path):
    """Connects to real Mongo, pings it, lists collections — smoke test."""
    from mongo_sync_agent.config import MongoConfig
    from mongo_sync_agent.mongo.connect import make_client, ping

    cfg = MongoConfig(database="test", uri=MONGO_URI)
    client = make_client(cfg)
    try:
        assert ping(client)
        db = client["test"]
        _ = db.list_collection_names()  # just must not raise
    finally:
        client.close()


class _FakeS3Uploader:
    """Stand-in for S3Uploader — records "uploads" without touching S3.

    Mirrors the subset of S3Uploader's interface that mongo/extract.py relies
    on: a `._prefix` attribute and an `.upload(local_path, key)` method
    returning an UploadResult.
    """

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix
        self.uploads: list[tuple[str, Path]] = []

    def upload(self, local_path, key):
        from mongo_sync_agent.s3 import UploadResult

        local_path = Path(local_path)
        size = local_path.stat().st_size
        self.uploads.append((key, local_path))
        return UploadResult(key=key, bytes_uploaded=size, etag="fake-etag")


def test_incremental_extraction_end_to_end(tmp_path):
    """
    1. Seed a test collection with 5 documents.
    2. Run extract_all → expect 5 docs, watermark set.
    3. Run again → expect 0 docs (no new data), watermark unchanged.
    4. Insert 3 more docs.
    5. Run again → expect 3 docs, watermark advanced.
    """
    import pymongo

    from mongo_sync_agent.config import CollectionConfig, MongoConfig
    from mongo_sync_agent.mongo.extract import extract_all
    from mongo_sync_agent.runner_types import RunContext
    from mongo_sync_agent.state import StateStore

    coll_name = f"msa_test_{uuid.uuid4().hex[:6]}"
    seed_client = pymongo.MongoClient(MONGO_URI)
    seed_db = seed_client["test"]

    try:
        seed_db[coll_name].insert_many([{"seq": i} for i in range(5)])

        mongo_cfg = MongoConfig(database="test", uri=MONGO_URI)
        coll_cfg = CollectionConfig(name=coll_name, mode="append_only")
        namespace = f"mongo:test.{coll_name}"

        state = StateStore(tmp_path / "state.db")
        uploader = _FakeS3Uploader()

        def make_run_ctx(run_id: str) -> RunContext:
            spool_dir = tmp_path / "spool" / run_id
            spool_dir.mkdir(parents=True, exist_ok=True)
            return RunContext(
                run_id=run_id,
                run_dt=datetime.now(timezone.utc),
                spool_dir=spool_dir,
            )

        try:
            # 1st run: 5 seeded docs are extracted, watermark advances from nothing.
            results = extract_all(mongo_cfg, [coll_cfg], state, uploader, make_run_ctx("run1"))
            assert len(results) == 1
            assert results[0].status == "ok"
            assert results[0].docs == 5

            wm_after_1 = state.get_watermark(namespace)
            assert wm_after_1 is not None
            assert wm_after_1.kind == "objectid"

            # 2nd run: no new data since watermark -> empty, watermark unchanged.
            results = extract_all(mongo_cfg, [coll_cfg], state, uploader, make_run_ctx("run2"))
            assert results[0].status == "empty"
            assert results[0].docs == 0

            wm_after_2 = state.get_watermark(namespace)
            assert wm_after_2 == wm_after_1

            # Insert 3 more docs.
            seed_db[coll_name].insert_many([{"seq": i} for i in range(5, 8)])

            # 3rd run: only the 3 new docs are picked up, watermark advances again.
            results = extract_all(mongo_cfg, [coll_cfg], state, uploader, make_run_ctx("run3"))
            assert results[0].status == "ok"
            assert results[0].docs == 3

            wm_after_3 = state.get_watermark(namespace)
            assert wm_after_3 is not None
            assert wm_after_3.value != wm_after_1.value
        finally:
            state.close()
    finally:
        seed_db.drop_collection(coll_name)
        seed_client.close()


@pytest.fixture(autouse=True)
def cleanup_test_collection():
    if not MONGO_URI:
        pytest.skip()
    import pymongo

    client = pymongo.MongoClient(MONGO_URI)
    yield
    client["test"].drop_collection("msa_test_collection")
    client.close()
