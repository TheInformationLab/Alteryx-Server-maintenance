"""Tests for mongo_sync_agent.s3: key formatting and the S3Uploader."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mongo_sync_agent.s3 import UploadError, hostmetrics_key, logs_key, mongo_key

from .conftest import FAKE_BUCKET, FakeS3Client

RUN_DT = datetime(2026, 7, 3, 12, 34, 56, tzinfo=timezone.utc)


def test_key_format_mongo():
    key = mongo_key("", "AlteryxService", "AS_Queue", RUN_DT)

    assert re.fullmatch(
        r"mongo/AlteryxService/AS_Queue/dt=\d{4}-\d{2}-\d{2}/part-\d{8}T\d{6}Z\.parquet",
        key,
    )
    assert key == "mongo/AlteryxService/AS_Queue/dt=2026-07-03/part-20260703T123456Z.parquet"


def test_key_format_with_prefix():
    key_no_slash = mongo_key("raw", "db", "coll", RUN_DT)
    key_with_slash = mongo_key("raw/", "db", "coll", RUN_DT)

    # Prefix is normalized to end with "/" regardless of how it was supplied.
    assert key_no_slash == key_with_slash
    assert key_no_slash.startswith("raw/mongo/")
    assert re.fullmatch(
        r"raw/mongo/db/coll/dt=\d{4}-\d{2}-\d{2}/part-\d{8}T\d{6}Z\.parquet",
        key_no_slash,
    )


def test_key_format_logs():
    key = logs_key("", "gallery", RUN_DT)

    assert re.fullmatch(
        r"logs/gallery/dt=\d{4}-\d{2}-\d{2}/part-\d{8}T\d{6}Z\.jsonl\.gz",
        key,
    )
    assert key == "logs/gallery/dt=2026-07-03/part-20260703T123456Z.jsonl.gz"


def test_key_format_hostmetrics():
    key = hostmetrics_key("", RUN_DT)

    assert re.fullmatch(
        r"hostmetrics/dt=\d{4}-\d{2}-\d{2}/part-\d{8}T\d{6}Z\.jsonl\.gz",
        key,
    )
    assert key == "hostmetrics/dt=2026-07-03/part-20260703T123456Z.jsonl.gz"


def test_upload_calls_boto3(tmp_path, fake_s3):
    fake_client, uploader = fake_s3

    local_file = tmp_path / "part.parquet"
    local_file.write_bytes(b"some parquet bytes")

    result = uploader.upload(local_file, "mongo/db/coll/dt=2026-07-03/part-x.parquet")

    # upload_file was called with the right bucket/key.
    assert len(fake_client.upload_calls) == 1
    filename, bucket, key = fake_client.upload_calls[0]
    assert Path(filename) == local_file
    assert bucket == FAKE_BUCKET
    assert key == "mongo/db/coll/dt=2026-07-03/part-x.parquet"

    # File actually landed where the fake says it would.
    uploaded_path = tmp_path / FAKE_BUCKET / "mongo/db/coll/dt=2026-07-03/part-x.parquet"
    assert uploaded_path.read_bytes() == b"some parquet bytes"

    assert result.key == "mongo/db/coll/dt=2026-07-03/part-x.parquet"
    assert result.bytes_uploaded == len(b"some parquet bytes")
    assert result.etag == "fake-etag"


def test_upload_error_on_client_error(tmp_path, fake_s3):
    fake_client, uploader = fake_s3
    fake_client.fail_on_nth_upload = 1

    local_file = tmp_path / "part.parquet"
    local_file.write_bytes(b"data")

    with pytest.raises(UploadError):
        uploader.upload(local_file, "mongo/db/coll/dt=2026-07-03/part-x.parquet")
