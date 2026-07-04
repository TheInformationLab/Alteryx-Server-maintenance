"""Test fixtures and fakes shared by the mongo-sync-agent test suite.

Contains:
    - ``FakeCollection``: a pymongo-``Collection``-shaped fake backed by an
      in-memory list of docs, whose ``find()`` returns a genuine generator
      (never a list/sequence) so that accidental materialisation of a cursor
      in production code (e.g. ``list(cursor)``, ``cursor[0]``, ``len(cursor)``)
      fails loudly in tests instead of silently working.
    - ``FakeS3Client``: a boto3-``S3.Client``-shaped fake that "uploads" files
      by copying them onto the local filesystem under a test-owned root, so
      assertions can be made on the bytes that would have been sent to S3.
    - Fixtures wiring both fakes (plus a real ``StateStore`` and spool
      directory) into pytest tests without hitting a real MongoDB, SQLite
      corruption risk, or AWS account.
"""

from __future__ import annotations

import operator
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

# src-layout package; make it importable without requiring an editable install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import botocore.exceptions

from mongo_sync_agent import s3 as s3_module
from mongo_sync_agent.s3 import S3Uploader
from mongo_sync_agent.state import StateStore

# ---------------------------------------------------------------------------
# FakeCollection
# ---------------------------------------------------------------------------

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "$gt": operator.gt,
    "$gte": operator.ge,
    "$lt": operator.lt,
    "$lte": operator.le,
    "$eq": operator.eq,
}


def _doc_matches(doc: dict, filt: dict) -> bool:
    """Return True if ``doc`` satisfies every field/operator clause in ``filt``.

    Supports the small subset of MongoDB query syntax the agent actually uses:
    an empty filter (matches everything), and single-field ``{field: {op:
    bound}}`` clauses using $gt/$gte/$lt/$lte/$eq. A field missing from the
    doc never matches a comparison operator.
    """
    if not filt:
        return True
    for field, cond in filt.items():
        if field not in doc:
            return False
        value = doc[field]
        if isinstance(cond, dict):
            for op, bound in cond.items():
                if op not in _OPS:
                    raise NotImplementedError(f"FakeCollection: unsupported operator {op!r}")
                if not _OPS[op](value, bound):
                    return False
        else:
            # Direct equality shorthand, e.g. {"field": "value"}.
            if value != cond:
                return False
    return True


def _apply_sort(docs: list[dict], sort: list[tuple[str, int]]) -> list[dict]:
    """Apply a pymongo-style sort spec (list of (field, direction) pairs).

    Later keys are the primary sort keys applied last (stable sort), matching
    pymongo's documented multi-key sort semantics.
    """
    result = docs
    for field, direction in reversed(sort):
        result = sorted(result, key=lambda d: d.get(field), reverse=(direction == -1))
    return result


class FakeCollection:
    """In-memory stand-in for a ``pymongo.collection.Collection``.

    Only the surface used by ``mongo_sync_agent.mongo.extract`` is
    implemented: ``find()`` with a filter dict, an optional sort spec, and the
    ``batch_size`` / ``no_cursor_timeout`` kwargs (accepted but unused — this
    fake never touches a network, so there is nothing to batch or time out).
    """

    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def find(
        self,
        filter: dict | None = None,
        sort: list[tuple[str, int]] | None = None,
        batch_size: int = 1000,
        no_cursor_timeout: bool = False,
    ):
        """Return a generator over matching docs — deliberately NOT a list.

        Filtering (and optional sorting) happens eagerly, right here, before
        the generator is handed back. That eagerness is intentional: it means
        the *laziness* of the returned object is solely about "this isn't a
        sequence you can index/len()/materialise for free", not about
        deferring the filter logic itself. Production code that does anything
        other than iterate the result (e.g. ``list(cursor)`` followed by
        indexing, or ``cursor[0]``) will fail exactly as it would against a
        real pymongo cursor used incorrectly.
        """
        filt = filter or {}
        matching = [doc for doc in self._docs if _doc_matches(doc, filt)]
        if sort:
            matching = _apply_sort(matching, sort)

        def _generator():
            for doc in matching:
                yield doc

        return _generator()


# ---------------------------------------------------------------------------
# FakeS3Client
# ---------------------------------------------------------------------------


class FakeS3Client:
    """In-memory/on-disk stand-in for a ``boto3.client("s3")`` object.

    ``upload_file`` copies the local file to ``<root>/<Bucket>/<Key>`` (creating
    parent directories as needed) so tests can assert on the bytes that would
    have reached S3. ``head_object`` returns a fixed fake ETag, matching the
    ``S3Uploader.upload`` code path that fetches the ETag after upload.
    """

    def __init__(self, root: Path, fail_on_nth_upload: int | None = None):
        self.root = Path(root)
        self.fail_on_nth_upload = fail_on_nth_upload
        self.upload_calls: list[tuple[str, str, str]] = []
        self._upload_count = 0

    @classmethod
    def FailOnNthUpload(cls, root: Path, n: int) -> "FakeS3Client":
        """Construct a FakeS3Client whose ``upload_file`` raises a ClientError
        on its ``n``-th invocation (1-indexed), succeeding normally otherwise.
        """
        return cls(root, fail_on_nth_upload=n)

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
        self._upload_count += 1
        self.upload_calls.append((Filename, Bucket, Key))

        if self.fail_on_nth_upload is not None and self._upload_count == self.fail_on_nth_upload:
            raise botocore.exceptions.ClientError(
                {
                    "Error": {
                        "Code": "InternalError",
                        "Message": "simulated upload failure (FakeS3Client)",
                    }
                },
                "PutObject",
            )

        dest = self.root / Bucket / Key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Filename, dest)

    def head_object(self, Bucket: str, Key: str) -> dict:
        # Real S3 ETags are quoted; S3Uploader.upload() strips the quotes.
        return {"ETag": '"fake-etag"'}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_BUCKET = "test-bucket"


@pytest.fixture
def tmp_state(tmp_path: Path):
    """A real StateStore opened against a throwaway SQLite file, closed after the test."""
    store = StateStore(tmp_path / "state.db")
    yield store
    store.close()


@pytest.fixture
def tmp_spool(tmp_path: Path) -> Path:
    """A throwaway spool directory."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    return spool_dir


@pytest.fixture
def fake_s3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A (FakeS3Client, S3Uploader) pair — the uploader's boto3 client is the fake.

    ``boto3.client`` is monkeypatched (module-local to ``mongo_sync_agent.s3``)
    so constructing the ``S3Uploader`` never touches real AWS credentials or
    the network.
    """
    fake_client = FakeS3Client(tmp_path)
    monkeypatch.setattr(s3_module.boto3, "client", lambda *a, **k: fake_client)

    uploader = S3Uploader(bucket=FAKE_BUCKET, prefix="", region="us-east-1")
    yield fake_client, uploader
