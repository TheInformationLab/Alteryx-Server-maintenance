"""Tests for byte-exact, offset-preserving log tailing (``logs/tailer.py``).

The invariants under test are the load-bearing ones from the module docstring:
only *complete* lines are returned, the trailing partial line is withheld, the
resume ``new_offset`` never lands mid-code-unit (always even for UTF-16LE), and
resuming from a returned offset never loses or duplicates a line.

Where a real example log is readable it is used as an integration-flavoured
fixture; the precise-assertion tests synthesise bytes directly so the exact
offsets are known.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mongo_sync_agent.logs.tailer import TailChunk, drain_file, tail_file

# A real Alteryx Gallery log (UTF-16LE, no BOM) if the example data is present.
REAL_GALLERY = (
    Path(__file__).resolve().parents[2]
    / "example_data"
    / "Alteryx"
    / "Alteryx"
    / "Gallery"
    / "Logs"
    / "alteryx-2026-04-26.csv"
)


def _write_utf16le(path: Path, text: str) -> int:
    """Write ``text`` as UTF-16LE (no BOM) and return the byte length."""
    data = text.encode("utf-16-le")
    path.write_bytes(data)
    return len(data)


def _write_utf8(path: Path, text: str) -> int:
    data = text.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def test_utf16le_tail_returns_complete_lines(tmp_path: Path):
    p = tmp_path / "log.csv"
    total = _write_utf16le(p, "alpha\nbeta\ngamma\n")

    chunk = tail_file(p, 0, "utf-16-le")

    assert chunk.lines == ["alpha", "beta", "gamma"]
    # File ends on a newline, so everything is consumed.
    assert chunk.new_offset == total
    assert chunk.new_offset % 2 == 0


def test_utf16le_partial_line_withheld(tmp_path: Path):
    p = tmp_path / "log.csv"
    # No trailing newline: "partial" is an incomplete final line.
    _write_utf16le(p, "one\ntwo\npartial")
    expected_offset = len("one\ntwo\n".encode("utf-16-le"))  # 16

    chunk = tail_file(p, 0, "utf-16-le")

    assert chunk.lines == ["one", "two"]
    assert "partial" not in chunk.lines
    # The offset stops right after the last complete line, leaving "partial".
    assert chunk.new_offset == expected_offset

    # A later poll, once the producer finishes the line, picks it up from there.
    _write_utf16le(p, "one\ntwo\npartial\n")
    chunk2 = tail_file(p, chunk.new_offset, "utf-16-le")
    assert chunk2.lines == ["partial"]


def test_utf16le_offset_always_even(tmp_path: Path):
    # Various contents, including odd-*character* counts, must never yield an
    # odd resume offset for a 2-byte encoding.
    contents = [
        "a\n",
        "abc\ndef\n",
        "x\ny\nz",  # trailing partial
        "single line no newline",
        "héllo\nwörld\n",  # multi-byte glyphs
    ]
    for i, text in enumerate(contents):
        p = tmp_path / f"log{i}.csv"
        _write_utf16le(p, text)
        chunk = tail_file(p, 0, "utf-16-le")
        assert chunk.new_offset % 2 == 0, f"odd offset for content {text!r}"

    # An odd *start* offset must be realigned forward to an even boundary.
    p = tmp_path / "odd_start.csv"
    _write_utf16le(p, "hello\nworld\n")
    chunk = tail_file(p, 1, "utf-16-le")
    assert chunk.new_offset % 2 == 0


def test_utf8_tail_basic(tmp_path: Path):
    p = tmp_path / "svc.log"
    total = _write_utf8(p, "a\nb\nc\n")

    chunk = tail_file(p, 0, "utf-8")
    assert chunk.lines == ["a", "b", "c"]
    assert chunk.new_offset == total

    # Trailing partial line is withheld.
    _write_utf8(p, "a\nb\npart")
    chunk2 = tail_file(p, 0, "utf-8")
    assert chunk2.lines == ["a", "b"]
    assert chunk2.new_offset == len("a\nb\n".encode("utf-8"))


def test_tail_resumes_from_offset(tmp_path: Path):
    p = tmp_path / "log.csv"
    lines = [f"line{i:02d}" for i in range(10)]
    _write_utf16le(p, "".join(l + "\n" for l in lines))

    # Bound the first read to exactly the first five lines' worth of bytes.
    first_five_bytes = len("".join(l + "\n" for l in lines[:5]).encode("utf-16-le"))

    chunk1 = tail_file(p, 0, "utf-16-le", max_bytes=first_five_bytes)
    assert chunk1.lines == lines[:5]
    assert chunk1.new_offset == first_five_bytes

    chunk2 = tail_file(p, chunk1.new_offset, "utf-16-le")
    assert chunk2.lines == lines[5:]

    assert chunk1.lines + chunk2.lines == lines


def test_tail_empty_file_returns_empty(tmp_path: Path):
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")

    chunk = tail_file(p, 0, "utf-16-le")
    assert chunk == TailChunk([], 0)


def test_tail_file_gone_returns_start(tmp_path: Path):
    missing = tmp_path / "does_not_exist.log"
    # utf-8 avoids the (even) alignment adjustment so the start is returned verbatim.
    chunk = tail_file(missing, 42, "utf-8")
    assert chunk == TailChunk([], 42)


def test_drain_file_exhausts_all_lines(tmp_path: Path):
    p = tmp_path / "log.csv"
    lines = [f"row-{i:03d}" for i in range(20)]
    total = _write_utf16le(p, "".join(l + "\n" for l in lines))

    # A small max_bytes forces several chunks.
    three_lines = len("".join(l + "\n" for l in lines[:3]).encode("utf-16-le"))
    chunks = drain_file(p, 0, "utf-16-le", max_bytes=three_lines)

    assert len(chunks) > 1  # genuinely drained across multiple reads
    all_lines = [line for c in chunks for line in c.lines]
    assert all_lines == lines
    assert chunks[-1].new_offset == total


def test_max_bytes_limits_per_call(tmp_path: Path):
    p = tmp_path / "big.csv"
    lines = [f"entry-{i:03d}" for i in range(20)]
    _write_utf16le(p, "".join(l + "\n" for l in lines))

    small = len("".join(l + "\n" for l in lines[:4]).encode("utf-16-le"))

    single = tail_file(p, 0, "utf-16-le", max_bytes=small)
    assert 0 < len(single.lines) < 20  # one call cannot return the whole backlog

    chunks = drain_file(p, 0, "utf-16-le", max_bytes=small)
    all_lines = [line for c in chunks for line in c.lines]
    assert all_lines == lines  # draining still returns everything


@pytest.mark.skipif(not REAL_GALLERY.exists(), reason="example gallery log not present")
def test_utf16le_real_fixture_tail(tmp_path: Path):
    # Sanity-check against a real Alteryx Gallery log (UTF-16LE, no BOM).
    chunk = tail_file(REAL_GALLERY, 0, "utf-16-le", max_bytes=65_536)
    assert chunk.lines, "expected to decode at least one line from the real log"
    assert chunk.new_offset % 2 == 0
    # The first line is the CSV header row.
    assert chunk.lines[0].startswith("Date,LogLevel")
