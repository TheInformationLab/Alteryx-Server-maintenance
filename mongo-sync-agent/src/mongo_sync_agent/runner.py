"""Top-level run-cycle orchestration for the mongo-sync-agent.

``run_cycle`` is the single entry point invoked by the CLI (and, indirectly,
by the scheduled task/cron job): it wires together config, locking, spool
setup, the three extraction modules (mongo, logs, hostmetrics), and the
state store's run-history bookkeeping, then returns a process exit code.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import AgentConfig
from .locking import AlreadyRunningError, SingleInstanceLock
from .logging_setup import PeakRssTracker, configure_logging
from .runner_types import RunContext, RunSummary, UnitResult
from .s3 import S3Uploader
from .spool import setup_spool, sweep_orphans
from .state import StateStore

logger = logging.getLogger(__name__)


def run_cycle(
    cfg: AgentConfig,
    only: set[str] | None = None,  # None = all modules; subset of {"mongo", "logs", "hostmetrics"}
    collection: str | None = None,  # if set, only extract this collection (mongo module only)
    dry_run: bool = False,
) -> int:
    """Run one full sync cycle.

    Returns:
        Process exit code: ``0`` if every unit succeeded (or was empty/
        skipped), ``1`` if at least one unit failed but the run otherwise
        completed, ``2`` on a fatal error (e.g. another instance already
        running, or an unhandled exception during the run).
    """
    # 1. Configure logging as early as possible so setup failures are captured too.
    run_id = str(uuid.uuid4())[:8]
    configure_logging(cfg.log_dir, run_id)

    # 2. Acquire the single-instance lock; refuse to run concurrently with another instance.
    lock = SingleInstanceLock(Path(cfg.spool_dir) / ".lock")
    try:
        lock.acquire()
    except AlreadyRunningError as e:
        logger.error("Agent already running (PID %d)", e.pid)
        return 2

    run_dt = datetime.now(timezone.utc)
    peak = PeakRssTracker()
    t0 = time.monotonic()

    try:
        # 3. Set up durable state and the per-run spool directory; sweep any
        #    leftover spool directories from crashed prior runs.
        state = StateStore(Path(cfg.state_db))
        spool_dir = setup_spool(cfg.spool_dir, run_id)
        sweep_orphans(cfg.spool_dir, run_id)

        run_ctx = RunContext(run_id=run_id, run_dt=run_dt, spool_dir=spool_dir, dry_run=dry_run)
        uploader = S3Uploader(cfg.s3.bucket, cfg.s3.prefix, cfg.s3.region)

        results: list[UnitResult] = []

        # 4. Mongo module.
        if (only is None or "mongo" in only) and cfg.mongo_enabled and cfg.mongo:
            from .mongo.extract import extract_all

            peak.sample()
            colls = cfg.collections
            if collection:
                colls = [c for c in colls if c.name == collection]
            results.extend(extract_all(cfg.mongo, colls, state, uploader, run_ctx))
            peak.sample()

        # 5. Logs module.
        if (only is None or "logs" in only) and cfg.logs.enabled:
            from .logs.ship import ship_all

            peak.sample()
            results.extend(ship_all(cfg.logs, state, uploader, run_ctx))
            peak.sample()

        # 6. Host metrics module.
        if (only is None or "hostmetrics" in only) and cfg.hostmetrics.enabled:
            from .hostmetrics.collect import collect
            from .s3 import hostmetrics_key
            from .sinks import GzipNdjsonSink
            from .spool import spool_path

            peak.sample()
            records = collect(cfg.hostmetrics.disks)
            if records:
                sp = spool_path(spool_dir, "hostmetrics", "jsonl.gz")
                sink = GzipNdjsonSink(sp)
                try:
                    sink.write_rows(records)
                except Exception:
                    sink.abort()
                    raise
                sink.close()  # step 1: finalise the gzip file on disk

                if dry_run:
                    sp.unlink(missing_ok=True)
                    results.append(UnitResult("hostmetrics", "ok", docs=len(records)))
                else:
                    try:
                        key = hostmetrics_key(uploader._prefix, run_dt)
                        upload_result = uploader.upload(sp, key)  # step 2: upload to S3
                    except Exception as e:
                        sink.abort()
                        logger.error("S3 upload failed for hostmetrics: %s", e)
                        results.append(UnitResult("hostmetrics", "failed", error=str(e)))
                    else:
                        sp.unlink(missing_ok=True)  # step 3: clean up spool (no state to commit)
                        results.append(
                            UnitResult(
                                "hostmetrics",
                                "ok",
                                docs=len(records),
                                bytes_uploaded=upload_result.bytes_uploaded,
                            )
                        )
            else:
                results.append(UnitResult("hostmetrics", "empty"))
            peak.sample()

        # 7. Build the run summary, persist it, and log a structured completion record.
        duration = time.monotonic() - t0
        summary = RunSummary(
            run_id=run_id,
            run_dt=run_dt.isoformat(),
            duration_s=round(duration, 2),
            peak_rss_bytes=peak.peak,
            units=results,
        )
        state.record_run(summary)
        state.close()

        logger.info(
            "Run complete",
            extra={
                "run_summary": {
                    "exit_code": summary.exit_code,
                    "duration_s": summary.duration_s,
                    "peak_rss_mb": round(summary.peak_rss_bytes / 1_048_576, 1),
                    "units": [{"unit": u.unit, "status": u.status, "docs": u.docs} for u in results],
                }
            },
        )
        return summary.exit_code

    except Exception:
        logger.exception("Fatal error in run cycle")
        return 2
    finally:
        lock.release()
