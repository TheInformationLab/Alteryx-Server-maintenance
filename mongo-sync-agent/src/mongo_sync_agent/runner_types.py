"""Shared run-scoped dataclasses.

These types are defined in their own module (rather than in ``runner.py``) so that
extraction modules -- ``mongo/extract.py``, the logs tailer, the host-metrics
collector -- can accept and return them without importing ``runner.py`` and
creating an import cycle (``runner`` imports the extractors, the extractors would
otherwise import ``runner``).

Nothing here has behaviour beyond the ``RunSummary.exit_code`` convenience
property; these are plain data carriers passed down the call stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass
class RunContext:
    """Immutable-ish context for a single agent run, threaded through every unit.

    Attributes:
        run_id: Unique identifier for this run (also the spool subdirectory name).
        run_dt: The run's UTC timestamp. Used both for S3 key partitioning and as
            the ``_extracted_at`` value stamped on every extracted row.
        spool_dir: The per-run spool directory where local files are staged before
            upload.
        dry_run: When True, files are written and finalized locally but neither
            uploaded to S3 nor committed to the watermark state.
        host: Logical identifier of the host this run is executing on (from
            ``AgentConfig.host_id``, else the machine's network name). Stamped
            onto every shipped log line and host-metric record so multi-host
            deployments can be disambiguated downstream.
    """

    run_id: str
    run_dt: datetime
    spool_dir: Path
    dry_run: bool = False
    host: str = ""


@dataclass
class UnitResult:
    """Outcome of extracting one unit (a collection, a log source, etc.).

    Status semantics:
        ok       -- data was extracted and (unless dry_run) uploaded.
        skipped  -- the unit was intentionally not processed.
        failed   -- an error prevented completion; ``error`` carries the message.
        empty    -- the unit ran cleanly but produced zero rows.
    """

    unit: str
    status: Literal["ok", "skipped", "failed", "empty"]
    docs: int = 0
    bytes_uploaded: int = 0
    error: str | None = None


@dataclass
class RunSummary:
    """Aggregate outcome of a whole run, persisted via ``StateStore.record_run``."""

    run_id: str
    run_dt: str
    duration_s: float
    peak_rss_bytes: int
    units: list[UnitResult]

    @property
    def exit_code(self) -> int:
        """Process exit code: 1 if any unit failed, else 0."""
        if any(u.status == "failed" for u in self.units):
            return 1
        return 0
