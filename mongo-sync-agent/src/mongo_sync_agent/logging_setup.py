"""Loguru-based logging configuration for the mongo-sync-agent.

Two sinks are set up each run:

* **File** (always DEBUG): a rotating log at ``{log_dir}/msa.log``, 10 MB per
  file, 5 compressed backups.  Captures every log call including DEBUG-level
  detail useful for diagnosing missed watermarks, rotation mismatches, etc.
* **Console** (stderr, at the operator-configured level, default ``WARNING``):
  quiet by default so normal scheduled-task runs produce no output unless
  something is wrong.  Set ``log_level = "DEBUG"`` or ``"INFO"`` in the
  ``[agent]`` config section to make the console more verbose.

Third-party libraries (``pymongo``, ``boto3``, ``botocore``) use stdlib
``logging``.  An :class:`InterceptHandler` is installed on the root stdlib
logger so their messages flow through loguru and appear in both sinks at the
correct level.

``run_id`` context
------------------
A :class:`contextvars.ContextVar` holds the current run ID.  A loguru filter
injects it into every record's ``extra`` dict, so the format string can
reference ``{extra[run_id]}`` without callers having to pass it explicitly.
Call :func:`set_run_id` (done automatically by :func:`configure_logging`) to
update the context variable at the start of each run.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
from pathlib import Path

import psutil
from loguru import logger

# ---------------------------------------------------------------------------
# Run-ID context variable
# ---------------------------------------------------------------------------

_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "msa_run_id", default="-"
)


def set_run_id(run_id: str) -> None:
    """Update the run ID that is injected into every log record."""
    _run_id_var.set(run_id)


def _inject_run_id(record: dict) -> bool:
    """Loguru filter that stamps ``extra["run_id"]`` on every record."""
    record["extra"]["run_id"] = _run_id_var.get()
    return True


# ---------------------------------------------------------------------------
# Log format strings
# ---------------------------------------------------------------------------

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level:<8}</level> | "
    "<cyan>{name}</cyan> | "
    "<dim>run={extra[run_id]}</dim> | "
    "{message}"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DDTHH:mm:ss.SSS!UTC}Z | {level:<8} | "
    "{name}:{function}:{line} | run={extra[run_id]} | {message}"
)


# ---------------------------------------------------------------------------
# Stdlib → loguru intercept (for boto3, pymongo, botocore, etc.)
# ---------------------------------------------------------------------------

class _InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk up the call stack to find the frame that issued the log call so
        # loguru can report the correct source location.
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(log_dir: str, run_id: str, level: str = "WARNING") -> None:
    """Configure loguru for one agent run.

    Removes any previously installed handlers, then adds:

    * A **file** sink at ``{log_dir}/msa.log`` (always DEBUG, 10 MB rotation,
      5 gzip-compressed backups retained).
    * A **console** (stderr) sink at *level* (default ``"WARNING"``).
    * A stdlib ``logging`` intercept so third-party library messages flow
      through loguru.

    Args:
        log_dir:  Directory in which to write ``msa.log``.  Created if absent.
        run_id:   Short identifier for this execution (8-char UUID prefix).
                  Injected into every log record automatically.
        level:    Minimum level for the console sink.  The file sink always
                  captures DEBUG regardless of this setting.
    """
    set_run_id(run_id)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / "msa.log"

    # Remove all existing loguru sinks (handles re-entrant calls in tests).
    logger.remove()

    # Console sink — quiet by default (WARNING), verbose when configured.
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=_CONSOLE_FORMAT,
        filter=_inject_run_id,
        colorize=True,
    )

    # File sink — always captures DEBUG-level detail for post-hoc diagnosis.
    logger.add(
        str(log_file),
        level="DEBUG",
        format=_FILE_FORMAT,
        filter=_inject_run_id,
        rotation="10 MB",
        retention=5,
        compression="gz",
        encoding="utf-8",
        backtrace=True,
        diagnose=False,  # diagnose=True would expose local variables; off for security
        enqueue=False,   # synchronous writes; agent is single-threaded
    )

    # Intercept stdlib logging (boto3, botocore, pymongo use it).
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    logger.debug(
        "Logging configured: console_level={} log_file={}",
        level.upper(),
        log_file,
    )


# ---------------------------------------------------------------------------
# Peak RSS tracker
# ---------------------------------------------------------------------------

class PeakRssTracker:
    """Track peak RSS memory usage across a run."""

    def __init__(self) -> None:
        self._peak: int = 0

    def sample(self) -> int:
        """Sample current RSS, update the internal peak, and return current RSS."""
        rss = psutil.Process(os.getpid()).memory_info().rss
        if rss > self._peak:
            self._peak = rss
        return rss

    @property
    def peak(self) -> int:
        """Peak RSS observed so far (bytes)."""
        return self._peak
