"""Ship tailed log lines to S3, one log source at a time.

CRASH SAFETY (write -> upload -> commit ordering):
    1. New lines are drained from every discovered file for this source and
       written to a local gzip-NDJSON spool file.
    2. The spool file is uploaded to S3 (a PUT is atomic -- no partial objects
       are ever visible to downstream readers).
    3. ONLY after the upload is confirmed are the per-file byte offsets
       committed to SQLite, in a single transaction covering every file
       touched this run.
If the process is killed between steps 2 and 3, the shipped lines already
exist in S3 but the offsets were never advanced, so the next run re-reads and
re-ships them under a new S3 key. This is the same at-least-once contract used
by the Mongo extractor (see ``mongo/extract.py``); downstream dedup on
(``source``, ``file``, ``file_offset``) is expected to handle the overlap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import LogsConfig, LogSourceConfig
from ..runner_types import RunContext, UnitResult
from ..s3 import S3Uploader, UploadError, logs_key
from ..sinks import GzipNdjsonSink
from ..spool import spool_path
from ..state import FileOffset, StateStore
from .sources import TailPlan, discover, gc_offsets
from .tailer import drain_file

logger = logging.getLogger(__name__)

_DEFAULT_GC_DAYS = 14  # falls back to LogsConfig's default if the caller doesn't pass one


def _provisional_key(path: Path) -> str:
    """Provisional state key for a not-yet-fingerprintable file.

    Mirrors :func:`mongo_sync_agent.logs.sources._provisional_key` (kept
    private there); duplicated here rather than imported since it is a
    one-line format shared as part of the on-disk key contract.
    """
    return f"path:{path}"


def ship_source(
    source_cfg: LogSourceConfig,
    state: StateStore,
    uploader: S3Uploader,
    run_ctx: RunContext,
    gc_days: int = _DEFAULT_GC_DAYS,
) -> UnitResult:
    """Ship new log lines from one configured log source.

    See the module docstring for the crash-safety ordering (write -> upload ->
    commit) this function upholds. That ordering must not change.

    Note: ``gc_days`` lives on :class:`~mongo_sync_agent.config.LogsConfig`
    (not on the per-source ``LogSourceConfig``), so :func:`ship_all` passes it
    through explicitly; callers that invoke ``ship_source`` directly get the
    same default (14 days) that ``LogsConfig`` uses.
    """
    state_offsets = state.get_offsets(source_cfg.name)
    plans = discover(source_cfg, state_offsets)

    records: list[dict] = []
    # Per-file bookkeeping, keyed by the same key used in state (fingerprint,
    # or a provisional "path:<path>" key for files too small to fingerprint).
    new_offsets: dict[str, tuple[TailPlan, int]] = {}  # key -> (plan, new_offset)
    current_keys: set[str] = set()

    for plan in plans:
        key = plan.fingerprint if plan.fingerprint is not None else _provisional_key(plan.path)
        current_keys.add(key)

        chunks = drain_file(plan.path, plan.start_offset, source_cfg.encoding, source_cfg.max_bytes_per_poll)

        offset = plan.start_offset
        for chunk in chunks:
            shipped_at = datetime.now(timezone.utc).isoformat()
            for line in chunk.lines:
                records.append(
                    {
                        "line": line,
                        "source": source_cfg.name,
                        "file": plan.path.name,
                        "file_offset": chunk.new_offset,
                        "shipped_at": shipped_at,
                    }
                )
            offset = chunk.new_offset

        new_offsets[key] = (plan, offset)

    if not records:
        return UnitResult(unit=source_cfg.name, status="empty")

    spool_file = spool_path(run_ctx.spool_dir, source_cfg.name, "jsonl.gz")
    sink = GzipNdjsonSink(spool_file)
    try:
        sink.write_rows(records)
    except Exception:
        sink.abort()
        raise

    result = sink.close()  # step 1: finalise the gzip file on disk

    if run_ctx.dry_run:
        sink.path.unlink(missing_ok=True)
        logger.info("dry_run: would upload %s (%d lines)", source_cfg.name, result.rows)
        return UnitResult(unit=source_cfg.name, status="ok", docs=result.rows, bytes_uploaded=0)

    try:
        key = logs_key(uploader._prefix, source_cfg.name, run_ctx.run_dt)
        upload_result = uploader.upload(spool_file, key)  # step 2: upload to S3
    except UploadError as e:
        sink.abort()
        logger.error("S3 upload failed for log source %s: %s", source_cfg.name, e)
        return UnitResult(unit=source_cfg.name, status="failed", docs=result.rows, error=str(e))

    # ONLY advance offsets after confirmed upload, all in one transaction:
    offsets_to_commit: list[FileOffset] = []
    for key, (plan, offset) in new_offsets.items():
        try:
            size_at_read = plan.path.stat().st_size
        except OSError:
            size_at_read = offset
        offsets_to_commit.append(
            FileOffset(
                fingerprint=key,
                last_path=str(plan.path),
                offset=offset,
                file_size_at_read=size_at_read,
                updated_at="",  # overwritten by StateStore.set_offsets
            )
        )
    state.set_offsets(source_cfg.name, offsets_to_commit, run_ctx.run_id)  # step 3: commit state

    # current_keys was captured during discovery: the set of fingerprint (or
    # provisional "path:") keys still present on disk this run.
    gc_offsets(state, source_cfg.name, current_keys, gc_days)

    spool_file.unlink(missing_ok=True)  # step 4: clean up spool (safe -- state already committed)
    return UnitResult(
        unit=source_cfg.name,
        status="ok",
        docs=result.rows,
        bytes_uploaded=upload_result.bytes_uploaded,
    )


def ship_all(
    logs_cfg: LogsConfig,
    state: StateStore,
    uploader: S3Uploader,
    run_ctx: RunContext,
) -> list[UnitResult]:
    """Ship all configured log sources. Failures are isolated per source."""
    if not logs_cfg.enabled:
        return []

    results: list[UnitResult] = []
    for source_cfg in logs_cfg.sources:
        try:
            results.append(ship_source(source_cfg, state, uploader, run_ctx, gc_days=logs_cfg.gc_days))
        except Exception as e:
            logger.exception("Unexpected error shipping source %s", source_cfg.name)
            results.append(UnitResult(unit=source_cfg.name, status="failed", error=str(e)))
    return results
