"""Real-filesystem tests for LocalLogRepository prune methods.

Uses pytest ``tmp_path`` to plant real files with manipulated mtimes and
verify that ``rotated_segments``, ``mtime``, and ``delete`` behave correctly
against actual pathlib / os.stat / unlink operations.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from service_orchestrator.modules.orchestrate.internal.local_log_repository import LocalLogRepository


def _setup_log_dir(tmp_path: Path, service: str = "docs") -> Path:
    """Create ``<tmp_path>/.winter/logs/`` and return its path."""
    log_dir = tmp_path / ".winter" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def test_rotated_segments_finds_numbered_files_not_active(tmp_path: Path) -> None:
    """rotated_segments returns .log.1 and .log.2 but not the active .log."""
    log_dir = _setup_log_dir(tmp_path)
    active = log_dir / "docs.log"
    seg1 = log_dir / "docs.log.1"
    seg2 = log_dir / "docs.log.2"
    active.write_text("active\n")
    seg1.write_text("seg1\n")
    seg2.write_text("seg2\n")

    repo = LocalLogRepository()
    result = repo.rotated_segments(tmp_path, "docs")

    assert active not in result
    assert seg1 in result
    assert seg2 in result


def test_rotated_segments_empty_when_no_rotated_files(tmp_path: Path) -> None:
    """rotated_segments returns [] when only the active log exists."""
    log_dir = _setup_log_dir(tmp_path)
    active = log_dir / "docs.log"
    active.write_text("active\n")

    repo = LocalLogRepository()
    result = repo.rotated_segments(tmp_path, "docs")

    assert result == []


def test_rotated_segments_empty_when_log_dir_absent(tmp_path: Path) -> None:
    """rotated_segments returns [] when the log directory does not exist."""
    repo = LocalLogRepository()
    result = repo.rotated_segments(tmp_path, "docs")
    assert result == []


def test_mtime_returns_correct_modification_time(tmp_path: Path) -> None:
    """mtime reads back the modification time set via os.utime."""
    log_dir = _setup_log_dir(tmp_path)
    seg = log_dir / "docs.log.1"
    seg.write_text("old\n")
    target_mtime = 1000.0
    os.utime(seg, (target_mtime, target_mtime))

    repo = LocalLogRepository()
    assert repo.mtime(seg) == pytest.approx(target_mtime)


def test_delete_removes_file(tmp_path: Path) -> None:
    """delete() unlinks an existing file."""
    log_dir = _setup_log_dir(tmp_path)
    seg = log_dir / "docs.log.1"
    seg.write_text("content\n")
    assert seg.exists()

    repo = LocalLogRepository()
    repo.delete(seg)

    assert not seg.exists()


def test_delete_is_noop_for_missing_file(tmp_path: Path) -> None:
    """delete() does not raise when the file is already gone."""
    log_dir = _setup_log_dir(tmp_path)
    absent = log_dir / "docs.log.99"

    repo = LocalLogRepository()
    repo.delete(absent)  # must not raise


def test_rotated_segments_excludes_non_numeric_suffix(tmp_path: Path) -> None:
    """rotated_segments excludes files with non-numeric suffixes (e.g. .bak)."""
    log_dir = _setup_log_dir(tmp_path)
    seg1 = log_dir / "docs.log.1"
    seg2 = log_dir / "docs.log.2"
    bak = log_dir / "docs.log.bak"
    seg1.write_text("seg1\n")
    seg2.write_text("seg2\n")
    bak.write_text("backup\n")

    repo = LocalLogRepository()
    result = repo.rotated_segments(tmp_path, "docs")

    assert seg1 in result
    assert seg2 in result
    assert bak not in result


def test_rotated_old_pruned_recent_kept(tmp_path: Path) -> None:
    """Plant two segments with different mtimes; verify which would be pruned.

    This is the integration scenario: old segment (60 days ago) should be
    prunable, recent segment (now) should be kept.
    """
    log_dir = _setup_log_dir(tmp_path)
    active = log_dir / "docs.log"
    old_seg = log_dir / "docs.log.2"
    recent_seg = log_dir / "docs.log.1"
    active.write_text("live\n")
    old_seg.write_text("old\n")
    recent_seg.write_text("recent\n")

    # Set old_seg mtime to 60 days ago
    sixty_days_ago = 1000.0
    os.utime(old_seg, (sixty_days_ago, sixty_days_ago))
    # recent_seg gets a very high mtime (far future, certainly recent)
    future = 9_999_999_999.0
    os.utime(recent_seg, (future, future))

    repo = LocalLogRepository()
    rotated = repo.rotated_segments(tmp_path, "docs")

    assert active not in rotated
    assert old_seg in rotated
    assert recent_seg in rotated

    # Simulate prune: cutoff = 5000.0 (recent_seg mtime 9e9 > 5000 → kept)
    cutoff = 5000.0
    to_delete = [p for p in rotated if repo.mtime(p) < cutoff]
    assert to_delete == [old_seg]

    # Actually delete and confirm
    for p in to_delete:
        repo.delete(p)

    assert not old_seg.exists()
    assert recent_seg.exists()
    assert active.exists()


# ---------------------------------------------------------------------------
# read_new_lines — byte-offset integrity under invalid/multibyte bytes
# ---------------------------------------------------------------------------


def test_read_new_lines_no_offset_drift_with_invalid_bytes(tmp_path: Path) -> None:
    """Offset arithmetic stays in the byte domain even when the log contains
    invalid / non-UTF-8 bytes.

    The logwriter decodes with errors="replace", which turns each invalid byte
    into U+FFFD (3 bytes when re-encoded).  The old (buggy) code re-encoded the
    decoded text to compute ``consumed``, so one invalid byte caused a +2
    over-count per bad byte, shifting the offset forward and making the next
    follow tick skip or duplicate lines.

    This test plants a log file in two flush cycles:
      - Tick 1 chunk: b"first\\xff\\n" — 8 raw bytes (\\xff is the invalid byte)
      - Tick 2 chunk: b"second\\n"    — 7 raw bytes

    After tick 1 the offset must advance by exactly 8 (bytes of the raw chunk
    up to and including the last \\n).  Tick 2 must then return ["second"],
    proving no lines were skipped or duplicated.
    """
    log_dir = _setup_log_dir(tmp_path)
    log_file = log_dir / "svc.log"

    # Write the first chunk with an embedded invalid byte.
    chunk1 = b"first\xff\n"
    log_file.write_bytes(chunk1)

    repo = LocalLogRepository()

    lines1, new_offset = repo.read_new_lines(log_file, 0)
    # Must return exactly one line; U+FFFD is the replacement for \xff.
    assert lines1 == ["first�"]
    # Offset must equal the raw byte count of chunk1 (8 bytes), not a re-encoded length.
    assert new_offset == len(chunk1)

    # Append a second chunk with only valid UTF-8.
    chunk2 = b"second\n"
    log_file.write_bytes(chunk1 + chunk2)

    lines2, final_offset = repo.read_new_lines(log_file, new_offset)
    # Second tick must return only the second line — no skip, no duplicate.
    assert lines2 == ["second"]
    assert final_offset == len(chunk1) + len(chunk2)


def test_read_new_lines_no_offset_drift_with_multibyte_utf8(tmp_path: Path) -> None:
    """Multi-byte UTF-8 characters (e.g. emoji, CJK) must not corrupt the offset.

    A 4-byte emoji (U+1F600) decodes to one character but re-encoding it still
    produces 4 bytes — so it does not trigger the drift bug itself.  However,
    this test confirms correct behaviour with multi-byte sequences generally.
    """
    log_dir = _setup_log_dir(tmp_path)
    log_file = log_dir / "svc.log"

    emoji = "\U0001f600"  # 4 bytes in UTF-8
    chunk1 = f"{emoji}\n".encode()
    log_file.write_bytes(chunk1)

    repo = LocalLogRepository()

    lines1, offset1 = repo.read_new_lines(log_file, 0)
    assert lines1 == [emoji]
    assert offset1 == len(chunk1)  # 5 bytes: 4 for emoji + 1 for \n

    chunk2 = b"next\n"
    log_file.write_bytes(chunk1 + chunk2)

    lines2, offset2 = repo.read_new_lines(log_file, offset1)
    assert lines2 == ["next"]
    assert offset2 == len(chunk1) + len(chunk2)
