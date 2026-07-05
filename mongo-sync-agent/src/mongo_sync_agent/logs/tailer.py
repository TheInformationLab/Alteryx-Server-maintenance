"""Byte-exact, offset-preserving tailing of rotated log files.

This module is load-bearing. It is handed a byte ``start`` offset (produced by
:mod:`mongo_sync_agent.logs.sources`) and must return only *complete* lines plus
the byte offset at which the next poll should resume. Two failure modes matter:

* **Data loss** -- advancing ``new_offset`` past bytes we never emitted skips
  log lines silently.
* **Corruption** -- emitting the trailing partial line (a line still being
  written by the producer) delivers a half-record that will never be corrected.

The defence against both is the same invariant: ``new_offset`` always points at
the byte *after* the last newline we actually consumed, and we never decode or
emit anything past that point. The trailing partial line is withheld until a
later poll sees its terminating newline.

Two on-disk encodings are handled:

* **Gallery logs** (``alteryx-YYYY-MM-DD[.N].csv``): UTF-16LE *without* a BOM.
  Every code unit is two bytes, so the newline is ``b"\\x0a\\x00"`` and every
  valid decode boundary is an *even* byte offset. Landing ``start`` or
  ``new_offset`` on an odd byte would split a code unit and corrupt the decode,
  so alignment is enforced defensively.
* **Service logs** (``AlteryxServiceLog_*.log``): UTF-8 / ASCII with a
  single-byte ``b"\\x0a"`` newline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

DEFAULT_MAX_BYTES = 8_388_608  # 8 MB -- bounds per-poll memory usage.

# Map (normalised) encoding name -> newline byte pattern on disk.
_NEWLINE_PATTERNS: dict[str, bytes] = {
    "utf-16-le": b"\x0a\x00",
    "utf-16le": b"\x0a\x00",
    "utf-8": b"\x0a",
    "utf8": b"\x0a",
    "ascii": b"\x0a",
    "latin-1": b"\x0a",
    "latin1": b"\x0a",
    "iso-8859-1": b"\x0a",
}

# Encodings whose code units are two bytes wide and must stay 2-byte aligned.
_TWO_BYTE_ENCODINGS = frozenset({"utf-16-le", "utf-16le"})


@dataclass
class TailChunk:
    """A batch of complete lines read from a log file in one bounded read.

    Attributes:
        lines: Complete lines, stripped of their line terminators. The trailing
            partial line (bytes after the last newline) is deliberately
            excluded and withheld for a later poll.
        new_offset: Byte offset immediately after the last complete line -- the
            offset to resume from next poll. Always a valid decode boundary
            (even, for UTF-16LE).
    """

    lines: list[str]
    new_offset: int


def _normalise(encoding: str) -> str:
    """Return a lowercased, whitespace-stripped encoding name for lookup."""
    return encoding.strip().lower().replace("_", "-")


def _newline_pattern(encoding: str) -> bytes:
    """Return the on-disk newline byte pattern for ``encoding``.

    Falls back to the single-byte LF ``b"\\x0a"`` (with a warning) for any
    encoding not explicitly known, which is the conservative choice: a
    single-byte search never skips over a real newline, it can only stop one
    short of a wider terminator.
    """
    key = _normalise(encoding)
    pattern = _NEWLINE_PATTERNS.get(key)
    if pattern is None:
        logger.warning(
            "Unknown encoding %r for newline detection; falling back to b'\\x0a'.",
            encoding,
        )
        return b"\x0a"
    return pattern


def tail_file(
    path: Path,
    start: int,
    encoding: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TailChunk:
    """Read up to ``max_bytes`` of new content from ``path`` starting at ``start``.

    Only complete lines are returned; the trailing partial line (if any) is
    withheld and its bytes are left unconsumed so a later poll can complete it.
    The returned :attr:`TailChunk.new_offset` is always a valid decode boundary
    (even, for UTF-16LE), so resuming from it never splits a code unit.

    When the read fills ``max_bytes`` the file has more backlog than one call can
    return; the caller should call again from ``new_offset`` (see
    :func:`drain_file`).

    Args:
        path: File to read.
        start: Byte offset to seek to before reading.
        encoding: Text encoding of the file (e.g. ``"utf-16-le"``, ``"utf-8"``).
        max_bytes: Maximum number of bytes to read in this call.

    Returns:
        A :class:`TailChunk`. On any condition where no complete line can be
        produced -- missing file, ``start`` past EOF, empty read, or no newline
        in the read window -- the chunk has empty ``lines`` and ``new_offset``
        equal to the (possibly alignment-adjusted) start.
    """
    nl = _newline_pattern(encoding)
    two_byte = _normalise(encoding) in _TWO_BYTE_ENCODINGS

    # Defensive alignment: a UTF-16LE offset must be even, or every subsequent
    # decode is shifted by one byte and corrupted. A prior buggy run could have
    # persisted an odd offset; realign by skipping the stray byte forward. We
    # move *forward* (never backward) so we never re-emit already-shipped bytes.
    if two_byte and start % 2 != 0:
        logger.warning(
            "Odd start offset %d for 2-byte encoding on '%s'; aligning to %d.",
            start,
            path,
            start + 1,
        )
        start += 1

    if start < 0:
        start = 0

    try:
        with open(path, "rb") as f:
            try:
                f.seek(start)
            except OSError:
                # Seek past a file shorter than start, or an unseekable handle.
                return TailChunk([], start)
            raw = f.read(max_bytes)
    except FileNotFoundError:
        # Vanished between discover() and here (rotation race): resume as-is.
        return TailChunk([], start)
    except OSError:
        logger.warning("Failed to read '%s' at offset %d.", path, start, exc_info=True)
        return TailChunk([], start)

    if not raw:
        # start is at or beyond EOF, or the file is empty: nothing new.
        return TailChunk([], start)

    # Find the LAST newline: everything up to and including it is complete; the
    # tail after it is a partial line we must withhold.
    #
    # For a 2-byte encoding the byte pattern b"\x0a\x00" can appear straddling a
    # code-unit boundary at an *odd* position -- e.g. U+0A41 (bytes 41 0a)
    # followed by U+4100 (bytes 00 41) yields "...41 0a 00 41...", a false
    # positive at an odd offset that is not a real newline. ``start`` is already
    # even here, so a valid boundary requires an even ``last_nl_pos``; search
    # backwards past any odd (spurious) matches to the last even-aligned one.
    last_nl_pos = raw.rfind(nl)
    if two_byte:
        while last_nl_pos != -1 and last_nl_pos % 2 != 0:
            last_nl_pos = raw.rfind(nl, 0, last_nl_pos)
    if last_nl_pos == -1:
        # No complete line at a valid boundary in this window: withhold the
        # whole (partial) window and do not advance.
        return TailChunk([], start)

    consumed_end = last_nl_pos + len(nl)
    consumed = raw[:consumed_end]
    new_offset = start + consumed_end

    if two_byte and new_offset % 2 != 0:
        # Unreachable given the even-aligned search above (even start + even
        # last_nl_pos + 2-byte newline), but assert the invariant loudly rather
        # than ever emit a corrupt resume boundary that would poison every poll.
        logger.error(
            "Computed odd new_offset %d for 2-byte encoding on '%s'; "
            "withholding chunk to avoid corrupting the resume boundary.",
            new_offset,
            path,
        )
        return TailChunk([], start)

    text = consumed.decode(encoding, errors="replace")

    # Normalise CRLF/CR then split. The terminating newline yields an empty
    # final element which we drop along with any other blank lines.
    lines = [
        line.rstrip("\r")
        for line in text.replace("\r\n", "\n").split("\n")
        if line.rstrip("\r") != ""
    ]

    return TailChunk(lines=lines, new_offset=new_offset)


def drain_file(
    path: Path,
    start: int,
    encoding: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[TailChunk]:
    """Repeatedly :func:`tail_file` until no more complete lines are available.

    Each iteration holds at most ``max_bytes`` in memory, so a large backlog is
    drained in bounded chunks rather than loaded whole. Iteration stops when a
    call returns no lines or fails to advance the offset (a safety guard against
    an accidental infinite loop).

    Args:
        path: File to drain.
        start: Byte offset to begin at.
        encoding: Text encoding of the file.
        max_bytes: Maximum bytes read per underlying :func:`tail_file` call.

    Returns:
        The ordered list of non-empty :class:`TailChunk` objects produced.
    """
    chunks: list[TailChunk] = []
    offset = start
    while True:
        chunk = tail_file(path, offset, encoding, max_bytes)
        if not chunk.lines:
            break
        chunks.append(chunk)
        if chunk.new_offset == offset:
            # No forward progress despite returning lines: stop rather than spin.
            break
        offset = chunk.new_offset
    return chunks
