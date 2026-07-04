"""Log source discovery and rotation-aware tailing plans.

This module is load-bearing: it decides *where* in each log file the next poll
should start reading. If it gets this wrong, log data is either silently
skipped (an offset advanced past unread bytes) or double-shipped (an offset
reset that should have been preserved).

The hard problem it solves is NLog-style size rotation as used by Alteryx
Gallery:

    The live file is ``alteryx-YYYY-MM-DD.csv``. When it grows past ~10 MB,
    NLog *renames* it to ``alteryx-YYYY-MM-DD.0.csv`` (``.1.csv`` and so on for
    successive rotations) and opens a fresh live file at the original path.

If offsets were keyed by path, the rename would strand the archive's offset and
we would re-ship the whole archive. Instead we key offsets by a *content
fingerprint* -- the sha256 of the first :data:`FP_PREFIX` bytes -- so a file we
were reading as ``alteryx-2026-04-01.csv`` is recognised as the same content
after it is renamed to ``alteryx-2026-04-01.0.csv``, and we resume from the
saved offset regardless of its current path.

Freshly rotated files are a corner case: until a file has accumulated
:data:`FP_PREFIX` bytes it cannot be fingerprinted (and every just-created file
sharing the same CSV header would collide on the same fingerprint anyway), so
such files are tracked *provisionally* by a path-derived key until they grow
big enough to fingerprint.
"""

from __future__ import annotations

import glob as glob_module
import hashlib
import logging
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import LogSourceConfig
from ..state import FileOffset, StateStore

logger = logging.getLogger(__name__)

FP_PREFIX = 4096  # bytes used for fingerprint (sha256 of first min(4096, file_size) bytes)


@dataclass
class TailPlan:
    """Where and how far to read a single discovered log file on this poll.

    Attributes:
        path: The current on-disk path of the file (may differ from the path
            we first saw it at, if it has since been rotated/renamed).
        fingerprint: Content fingerprint (sha256 hex of the first
            :data:`FP_PREFIX` bytes), or ``None`` if the file is still too
            small to fingerprint and is therefore tracked provisionally by
            path.
        start_offset: Byte offset to ``seek`` to before reading.
    """

    path: Path
    fingerprint: str | None
    start_offset: int


def _provisional_key(path: Path) -> str:
    """Return the state key used to track a not-yet-fingerprintable file.

    All freshly rotated files share the same header bytes, so they cannot be
    distinguished by content until they exceed :data:`FP_PREFIX`. Until then we
    key them by their path string so each is tracked independently.
    """
    return f"path:{path}"


def fingerprint(path: Path) -> str | None:
    """Compute the content fingerprint of a file.

    Reads exactly ``min(FP_PREFIX, file_size)`` bytes in binary mode and
    returns their sha256 hex digest. Returns ``None`` when the file is smaller
    than :data:`FP_PREFIX` bytes, because a short prefix is not distinctive
    enough (every just-rotated file would share the same header) to safely key
    an offset on.

    Args:
        path: File to fingerprint.

    Returns:
        Lowercase hex sha256 digest of the first :data:`FP_PREFIX` bytes, or
        ``None`` if the file is smaller than :data:`FP_PREFIX`.
    """
    size = path.stat().st_size
    if size < FP_PREFIX:
        return None
    with open(path, "rb") as f:
        data = f.read(FP_PREFIX)
    return hashlib.sha256(data).hexdigest()


def discover(
    source: LogSourceConfig,
    state_offsets: dict[str, FileOffset],  # keyed by fingerprint (and provisional "path:" keys)
) -> list[TailPlan]:
    """Build an ordered list of :class:`TailPlan` for one log source.

    Files are returned oldest-first (sorted by mtime, then name) so that
    renamed archives are shipped before the live file, preserving chronological
    order within the source. The live file -- being the most recently written
    -- therefore sorts last.

    For each file matched by ``source.path_glob``:

    * If the file is large enough to fingerprint (``size >= FP_PREFIX``):
        - **Known fingerprint** -> the same content we have seen before, even
          if it has since been renamed by rotation. Resume from the stored
          offset. If the file is now *smaller* than that stored offset the file
          was truncated/recreated; log a warning and reset to ``0``.
        - **Unknown fingerprint** -> a new file (or a previously provisional
          file that has now grown past :data:`FP_PREFIX`); start from ``0``.
    * If the file is still too small to fingerprint -> track it provisionally
      by a path-derived key, carrying any existing provisional offset, and mark
      ``fingerprint=None`` on the plan.

    Files that disappear between the glob and the stat/read (a rotation race)
    are skipped; the next poll will pick them up under their new name.

    Args:
        source: The log source whose files to discover.
        state_offsets: Previously stored offsets for this source, keyed by
            fingerprint. Provisional records are keyed by ``"path:<path>"``.

    Returns:
        Plans ordered oldest-first (by mtime, then name).
    """
    plans: list[TailPlan] = []
    entries: list[tuple[float, str, Path]] = []

    for match in glob_module.glob(source.path_glob):
        path = Path(match)
        try:
            st = path.stat()
        except OSError:
            # Vanished between glob and stat (e.g. rotated away): skip this poll.
            continue
        if not stat_module.S_ISREG(st.st_mode):
            continue
        entries.append((st.st_mtime, str(path), path))

    # Oldest first: archives (older mtime) before the live file (newest mtime).
    entries.sort(key=lambda e: (e[0], e[1]))

    for mtime, _name, path in entries:
        try:
            fp = fingerprint(path)
        except OSError:
            # Rotated/removed while we were reading its prefix: skip this poll.
            continue

        if fp is not None:
            existing = state_offsets.get(fp)
            if existing is not None:
                # Known content, possibly under a new name after rotation.
                start = existing.offset
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < existing.offset:
                    logger.warning(
                        "Truncation detected for source '%s': file '%s' "
                        "(fingerprint %s) is %d bytes but saved offset is %d; "
                        "resetting to 0.",
                        source.name,
                        path,
                        fp,
                        size,
                        existing.offset,
                    )
                    start = 0
                plans.append(TailPlan(path=path, fingerprint=fp, start_offset=start))
            else:
                # New content, or a provisional file that has now grown past
                # FP_PREFIX. Either way it is a new fingerprint: start at 0.
                # (Re-shipping at most the header row is acceptable; the stale
                # provisional "path:" record, if any, is reaped by gc_offsets.)
                plans.append(TailPlan(path=path, fingerprint=fp, start_offset=0))
        else:
            # Too small to fingerprint yet: track provisionally by path.
            key = _provisional_key(path)
            existing = state_offsets.get(key)
            start = 0
            if existing is not None:
                start = existing.offset
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < existing.offset:
                    logger.warning(
                        "Truncation detected for source '%s': provisional file "
                        "'%s' is %d bytes but saved offset is %d; resetting to 0.",
                        source.name,
                        path,
                        size,
                        existing.offset,
                    )
                    start = 0
            plans.append(TailPlan(path=path, fingerprint=None, start_offset=start))

    return plans


def gc_offsets(
    state: StateStore,
    source: str,
    current_fingerprints: set[str],
    gc_days: int = 14,
) -> int:
    """Delete stale stored offsets for a source.

    An offset is deleted when both are true:

    * its key (fingerprint, or provisional ``"path:"`` key) is **not** in
      ``current_fingerprints`` -- i.e. no file currently on disk maps to it, so
      it will never be resumed; and
    * its ``updated_at`` is older than ``gc_days`` -- a grace period so a file
      that is merely absent this poll (mid-rotation, temporarily moved) is not
      forgotten prematurely.

    Records with an unparseable ``updated_at`` are left in place (and warned
    about) rather than deleted blindly.

    Args:
        state: The state store to prune.
        source: Log source name.
        current_fingerprints: Keys still in use this run (fingerprints of
            fingerprintable files plus provisional ``"path:"`` keys for
            still-small files).
        gc_days: Age threshold in days before an unreferenced offset is
            eligible for deletion.

    Returns:
        Number of offset records deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=gc_days)
    deleted = 0

    for key, fo in state.get_offsets(source).items():
        if key in current_fingerprints:
            continue

        try:
            updated = datetime.fromisoformat(fo.updated_at)
        except ValueError:
            logger.warning(
                "Skipping gc for source '%s' key '%s': unparseable updated_at %r.",
                source,
                key,
                fo.updated_at,
            )
            continue

        # Stored timestamps are UTC; tolerate a naive value defensively.
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)

        if updated < cutoff:
            state.delete_offset(source, key)
            deleted += 1

    return deleted
