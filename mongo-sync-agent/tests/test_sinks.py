"""Tests for the streaming row sinks.

These pin the sink half of the memory invariant (one row group per
``write_rows`` call), round-trip correctness, and the abort contract (a failed
run must leave no file behind).
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

import pyarrow.parquet as pq
from bson import ObjectId, json_util

from mongo_sync_agent.config import CollectionConfig
from mongo_sync_agent.landing import RowContext, VariantJsonLanding
from mongo_sync_agent.mongo.watermark import ObjectIdWatermark
from mongo_sync_agent.sinks import GzipNdjsonSink, ParquetVariantSink


def _landing() -> VariantJsonLanding:
    cfg = CollectionConfig(name="test", mode="append_only")
    return VariantJsonLanding(ObjectIdWatermark(cfg))


def _rows(landing: VariantJsonLanding, docs: list[dict]) -> list[dict]:
    ctx = RowContext(extracted_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    return [landing.doc_to_row(d, ctx) for d in docs]


# --------------------------------------------------------------------------- #
# ParquetVariantSink
# --------------------------------------------------------------------------- #

def test_parquet_n_batches_n_rowgroups(tmp_path):
    landing = _landing()
    path = tmp_path / "out.parquet"
    sink = ParquetVariantSink(path, landing)

    n_batches, m_rows = 5, 4
    for _ in range(n_batches):
        docs = [{"_id": ObjectId(), "v": i} for i in range(m_rows)]
        sink.write_rows(_rows(landing, docs))
    sink.close()

    meta = pq.read_metadata(str(path))
    assert meta.num_row_groups == n_batches
    assert meta.num_rows == n_batches * m_rows


def test_parquet_roundtrip(tmp_path):
    landing = _landing()
    path = tmp_path / "out.parquet"
    sink = ParquetVariantSink(path, landing)

    docs = [{"_id": ObjectId(), "name": f"item-{i}", "n": i} for i in range(6)]
    sink.write_rows(_rows(landing, docs))
    sink.close()

    table = pq.read_table(str(path))
    payloads = table.column("payload").to_pylist()
    ids = table.column("_id").to_pylist()

    assert len(payloads) == len(docs)
    for doc, payload, stored_id in zip(docs, payloads, ids):
        decoded = json_util.loads(payload)
        assert decoded["name"] == doc["name"]
        assert decoded["n"] == doc["n"]
        assert stored_id == str(doc["_id"])


def test_parquet_abort_no_file(tmp_path):
    landing = _landing()
    path = tmp_path / "out.parquet"
    sink = ParquetVariantSink(path, landing)

    sink.write_rows(_rows(landing, [{"_id": ObjectId()}]))
    assert path.exists()  # writer created the file

    sink.abort()
    assert not path.exists()


def test_parquet_abort_no_writer_is_noop(tmp_path):
    # abort() before any write must not raise and must leave no file.
    path = tmp_path / "never.parquet"
    sink = ParquetVariantSink(path, _landing())
    sink.abort()
    assert not path.exists()


def test_parquet_rows_count(tmp_path):
    landing = _landing()
    sink = ParquetVariantSink(tmp_path / "out.parquet", landing)

    assert sink.rows == 0
    sink.write_rows(_rows(landing, [{"_id": ObjectId()} for _ in range(3)]))
    assert sink.rows == 3
    sink.write_rows([])  # empty batch is a no-op
    assert sink.rows == 3
    sink.write_rows(_rows(landing, [{"_id": ObjectId()} for _ in range(2)]))
    assert sink.rows == 5

    result = sink.close()
    assert result.rows == 5


# --------------------------------------------------------------------------- #
# GzipNdjsonSink
# --------------------------------------------------------------------------- #

def test_gzip_ndjson_roundtrip(tmp_path):
    path = tmp_path / "out.jsonl.gz"
    sink = GzipNdjsonSink(path)

    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}, {"a": 3, "b": "z"}]
    sink.write_rows(rows[:2])
    sink.write_rows(rows[2:])
    result = sink.close()

    assert result.rows == 3
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    assert lines == rows


def test_gzip_ndjson_abort_no_file(tmp_path):
    path = tmp_path / "out.jsonl.gz"
    sink = GzipNdjsonSink(path)
    sink.write_rows([{"a": 1}])
    assert path.exists()  # gzip file handle opened on construction

    sink.abort()
    assert not path.exists()
