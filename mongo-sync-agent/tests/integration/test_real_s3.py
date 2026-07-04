"""Integration tests against a real S3 bucket.

Skipped unless MSA_TEST_S3_BUCKET is set. Point it at a disposable/test-only
bucket (or prefix) — this test uploads and then deletes a small object.
"""

from __future__ import annotations

import os

import pytest

S3_BUCKET = os.environ.get("MSA_TEST_S3_BUCKET")
pytestmark = pytest.mark.skipif(not S3_BUCKET, reason="Set MSA_TEST_S3_BUCKET to run")


def test_upload_and_key_format(tmp_path):
    """Upload a small file, verify it lands at the expected key."""
    from datetime import datetime, timezone

    from mongo_sync_agent.s3 import S3Uploader, mongo_key

    uploader = S3Uploader(S3_BUCKET)
    test_file = tmp_path / "test.parquet"
    test_file.write_bytes(b"fake parquet content")

    run_dt = datetime.now(timezone.utc)
    key = mongo_key("msa-test/", "testdb", "testcoll", run_dt)
    result = uploader.upload(test_file, key)

    assert result.key == key
    assert result.bytes_uploaded == test_file.stat().st_size

    # Cleanup
    import boto3

    boto3.client("s3").delete_object(Bucket=S3_BUCKET, Key=key)
