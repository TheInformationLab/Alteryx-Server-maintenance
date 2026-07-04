"""S3 uploader using boto3.

Credentials come from the standard boto3 chain: environment variables, IAM
instance profiles, or named profiles. No credentials are hardcoded.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import boto3
import botocore.exceptions

logger = logging.getLogger(__name__)


class UploadError(Exception):
    """Raised when S3 upload fails."""

    pass


class UploadResult(NamedTuple):
    """Result of a successful S3 upload."""

    key: str
    bytes_uploaded: int
    etag: str


class S3Uploader:
    """Upload local files to S3."""

    def __init__(
        self, bucket: str, prefix: str = "", region: str = "us-east-1"
    ) -> None:
        """Initialize S3 uploader.

        Args:
            bucket: S3 bucket name.
            prefix: Optional S3 key prefix (normalized to end with "/" if non-empty).
            region: AWS region (default: us-east-1).
        """
        self._bucket = bucket
        self._region = region

        # Normalize prefix: ensure it ends with "/" if non-empty
        self._prefix = prefix
        if self._prefix and not self._prefix.endswith("/"):
            self._prefix += "/"

        # Create S3 client; credentials come from boto3 chain
        # (env vars, IAM instance profile, ~/.aws/credentials, etc.)
        self._s3 = boto3.client("s3", region_name=region)

    def upload(self, local_path: Path | str, key: str) -> UploadResult:
        """Upload a local file to S3.

        Args:
            local_path: Path to local file.
            key: S3 object key (full path, including any prefix).

        Returns:
            UploadResult with key, bytes uploaded, and ETag.

        Raises:
            UploadError: If upload fails.
        """
        local_path = Path(local_path)

        try:
            # Use TransferManager via upload_file for multipart handling
            self._s3.upload_file(str(local_path), self._bucket, key)
            logger.debug(
                f"Uploaded {local_path} to s3://{self._bucket}/{key}"
            )

            # Fetch ETag for result
            response = self._s3.head_object(Bucket=self._bucket, Key=key)
            etag = response["ETag"].strip('"')

            # Get file size
            bytes_uploaded = local_path.stat().st_size

            return UploadResult(key=key, bytes_uploaded=bytes_uploaded, etag=etag)

        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
            msg = f"Failed to upload {local_path} to s3://{self._bucket}/{key}: {e}"
            logger.error(msg)
            raise UploadError(msg) from e


def mongo_key(prefix: str, db: str, coll: str, run_dt: datetime) -> str:
    """Build S3 key for MongoDB collection export.

    Args:
        prefix: S3 prefix (empty or normalized with trailing "/").
        db: MongoDB database name.
        coll: MongoDB collection name.
        run_dt: Run datetime (UTC).

    Returns:
        S3 key: "{prefix}mongo/{db}/{coll}/dt={YYYY-MM-DD}/part-{YYYYMMDDTHHMMSSZ}.parquet"
    """
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    dt_partition = run_dt.strftime("%Y-%m-%d")
    run_ts = run_dt.strftime("%Y%m%dT%H%M%SZ")

    return f"{prefix}mongo/{db}/{coll}/dt={dt_partition}/part-{run_ts}.parquet"


def logs_key(prefix: str, source: str, run_dt: datetime) -> str:
    """Build S3 key for log source export.

    Args:
        prefix: S3 prefix (empty or normalized with trailing "/").
        source: Log source name.
        run_dt: Run datetime (UTC).

    Returns:
        S3 key: "{prefix}logs/{source}/dt={YYYY-MM-DD}/part-{YYYYMMDDTHHMMSSZ}.jsonl.gz"
    """
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    dt_partition = run_dt.strftime("%Y-%m-%d")
    run_ts = run_dt.strftime("%Y%m%dT%H%M%SZ")

    return f"{prefix}logs/{source}/dt={dt_partition}/part-{run_ts}.jsonl.gz"


def hostmetrics_key(prefix: str, run_dt: datetime) -> str:
    """Build S3 key for host metrics export.

    Args:
        prefix: S3 prefix (empty or normalized with trailing "/").
        run_dt: Run datetime (UTC).

    Returns:
        S3 key: "{prefix}hostmetrics/dt={YYYY-MM-DD}/part-{YYYYMMDDTHHMMSSZ}.jsonl.gz"
    """
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    dt_partition = run_dt.strftime("%Y-%m-%d")
    run_ts = run_dt.strftime("%Y%m%dT%H%M%SZ")

    return f"{prefix}hostmetrics/dt={dt_partition}/part-{run_ts}.jsonl.gz"
