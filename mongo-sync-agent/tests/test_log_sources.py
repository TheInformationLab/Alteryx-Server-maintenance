"""Tests for rotation-aware log-source discovery (``logs/sources.py``).

These exercise the load-bearing offset-placement decisions ``discover`` makes,
using ``tmp_path`` as a stand-in filesystem and a real :class:`StateStore` for
the persisted offsets:

* NLog-style *rename* rotation must carry a file's byte offset across its
  rename (offsets are keyed by content fingerprint, not path), so the renamed
  archive resumes where we left off while the fresh live file starts at 0.
* A file that shrank below its saved offset (truncation / recreation) must
  reset to 0 and warn rather than seek past EOF.
* A never-before-seen fingerprint starts at 0.
* A file too small to fingerprint is tracked provisionally (``fingerprint=None``).
* Stale, unreferenced offsets are garbage-collected once past the grace period.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mongo_sync_agent.config import LogSourceConfig
from mongo_sync_agent.logs.sources import FP_PREFIX, discover, fingerprint, gc_offsets
from mongo_sync_agent.state import FileOffset, StateStore

SOURCE = "gallery"


def _source(tmp_path: Path) -> LogSourceConfig:
    return LogSourceConfig(
        name=SOURCE,
        path_glob=str(tmp_path / "alteryx-*.csv"),
        encoding="utf-16-le",
    )


def _fingerprintable_bytes(tag: str, nlines: int = 20) -> bytes:
    """Return >= FP_PREFIX bytes of distinctive content.

    The ``tag`` makes the leading FP_PREFIX bytes unique per file, so two files
    built with different tags fingerprint differently (mirroring how a freshly
    rotated live file, full of new log lines, differs from its archived
    predecessor in its first bytes).
    """
    # ~220 bytes per line * 20 lines comfortably exceeds FP_PREFIX (4096).
    lines = [f"{tag}-line-{i:04d}-" + "x" * 200 for i in range(nlines)]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    assert len(data) >= FP_PREFIX, "fixture must exceed FP_PREFIX to be fingerprintable"
    return data


def _store_offset(state: StateStore, fp: str, path: Path, offset: int) -> None:
    state.set_offsets(
        SOURCE,
        [
            FileOffset(
                fingerprint=fp,
                last_path=str(path),
                offset=offset,
                file_size_at_read=offset,
                updated_at="",  # overwritten by StateStore.set_offsets
            )
        ],
        "run-1",
    )


def test_rename_rotation_carries_offset(tmp_path: Path, tmp_state: StateStore):
    # 1. Create a live file with 20 lines, large enough to fingerprint.
    live = tmp_path / "alteryx-2026-04-01.csv"
    live.write_bytes(_fingerprintable_bytes("A"))

    source = _source(tmp_path)

    # 2. First discover with no state: a single plan, starting at 0.
    plans = discover(source, tmp_state.get_offsets(SOURCE))
    assert len(plans) == 1
    assert plans[0].start_offset == 0
    assert plans[0].fingerprint is not None

    # 3. "Tail" it: record the offset as the full file size.
    fp_live = fingerprint(live)
    size = live.stat().st_size

    # 4. Persist that offset under the file's fingerprint.
    _store_offset(tmp_state, fp_live, live, size)

    # 5. Simulate NLog rename rotation: live -> .0.csv, fresh live created.
    archive = tmp_path / "alteryx-2026-04-01.0.csv"
    live.rename(archive)
    live.write_bytes(_fingerprintable_bytes("B"))  # different first bytes -> new fp
    # Ensure the fresh live file sorts newest (discover orders oldest-first).
    time.sleep(0.01)
    os.utime(live, None)

    # 6. Discover again: the archive keeps the carried offset; the live file,
    #    a genuinely new fingerprint, starts at 0.
    plans2 = discover(source, tmp_state.get_offsets(SOURCE))
    by_name = {p.path.name: p for p in plans2}
    assert set(by_name) == {archive.name, live.name}

    archive_plan = by_name[archive.name]
    assert archive_plan.fingerprint == fp_live
    assert archive_plan.start_offset == size
    assert archive_plan.start_offset > 0

    live_plan = by_name[live.name]
    assert live_plan.fingerprint is not None
    assert live_plan.fingerprint != fp_live
    assert live_plan.start_offset == 0

    # Oldest-first ordering: the archive (older mtime) precedes the live file.
    assert [p.path.name for p in plans2] == [archive.name, live.name]


def test_truncation_resets_offset(tmp_path: Path, tmp_state: StateStore, caplog):
    # 1. Create a file large enough to fingerprint, and fingerprint it. It is
    #    deliberately larger than FP_PREFIX so that a later truncation which
    #    keeps the first FP_PREFIX bytes leaves the fingerprint stable (the file
    #    is still recognised as the same content, just shorter).
    path = tmp_path / "alteryx-2026-05-01.csv"
    path.write_bytes(_fingerprintable_bytes("C", nlines=40))  # ~8.8 KB
    fp = fingerprint(path)
    original_size = path.stat().st_size

    # Persist an offset near the end of the original file.
    saved_offset = original_size
    _store_offset(tmp_state, fp, path, saved_offset)

    # 2. Truncate the file to exactly FP_PREFIX bytes: still fingerprintable
    #    (and to the SAME fingerprint, since the first FP_PREFIX bytes are
    #    unchanged), but now far smaller than the saved offset.
    with open(path, "r+b") as f:
        f.truncate(FP_PREFIX)
    assert fingerprint(path) == fp  # same content prefix -> same key
    assert path.stat().st_size < saved_offset

    # 3. Discover must detect the shrink, reset to 0, and warn.
    source = _source(tmp_path)
    with caplog.at_level(logging.WARNING, logger="mongo_sync_agent.logs.sources"):
        plans = discover(source, tmp_state.get_offsets(SOURCE))

    assert len(plans) == 1
    assert plans[0].start_offset == 0
    assert any("Truncation detected" in rec.message for rec in caplog.records)


def test_new_file_starts_at_zero(tmp_path: Path, tmp_state: StateStore):
    # An empty state + a fingerprintable file whose fingerprint we've never
    # seen must start from the beginning.
    path = tmp_path / "alteryx-2026-06-01.csv"
    path.write_bytes(_fingerprintable_bytes("NEW"))

    plans = discover(_source(tmp_path), tmp_state.get_offsets(SOURCE))

    assert len(plans) == 1
    assert plans[0].fingerprint is not None  # large enough to fingerprint
    assert plans[0].start_offset == 0


def test_small_file_provisional_key(tmp_path: Path, tmp_state: StateStore):
    # A file below FP_PREFIX cannot be fingerprinted; it is tracked
    # provisionally (fingerprint=None on the plan) until it grows.
    path = tmp_path / "alteryx-2026-06-02.csv"
    path.write_bytes(b"tiny header only\n")
    assert path.stat().st_size < FP_PREFIX
    assert fingerprint(path) is None

    plans = discover(_source(tmp_path), tmp_state.get_offsets(SOURCE))

    assert len(plans) == 1
    assert plans[0].fingerprint is None
    assert plans[0].start_offset == 0


def test_gc_removes_stale_offsets(tmp_path: Path, tmp_state: StateStore):
    # Persist an offset, then backdate its updated_at to 20 days ago so it is
    # past the default 14-day grace period.
    fp = "deadbeef" * 8  # arbitrary fingerprint no longer present on disk
    _store_offset(tmp_state, fp, tmp_path / "gone.csv", 4096)

    stale = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    # The connection is in autocommit mode, so this UPDATE persists immediately.
    tmp_state._conn.execute(
        "UPDATE file_offsets SET updated_at = ? WHERE source = ?",
        (stale, SOURCE),
    )
    assert set(tmp_state.get_offsets(SOURCE)) == {fp}

    # No file on disk maps to this fingerprint this run -> eligible for GC.
    deleted = gc_offsets(tmp_state, SOURCE, current_fingerprints=set(), gc_days=14)

    assert deleted == 1
    assert tmp_state.get_offsets(SOURCE) == {}


def test_gc_keeps_recent_and_referenced_offsets(tmp_path: Path, tmp_state: StateStore):
    # A guard around test_gc_removes_stale_offsets: neither a still-referenced
    # key nor a fresh (within-grace) key should be collected.
    referenced = "a" * 40
    fresh = "b" * 40
    _store_offset(tmp_state, referenced, tmp_path / "ref.csv", 10)
    _store_offset(tmp_state, fresh, tmp_path / "fresh.csv", 20)

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    # Backdate only the referenced one; it must still survive because it is
    # named in current_fingerprints.
    tmp_state._conn.execute(
        "UPDATE file_offsets SET updated_at = ? WHERE source = ? AND fingerprint = ?",
        (old, SOURCE, referenced),
    )

    deleted = gc_offsets(tmp_state, SOURCE, current_fingerprints={referenced}, gc_days=14)

    assert deleted == 0
    assert set(tmp_state.get_offsets(SOURCE)) == {referenced, fresh}
