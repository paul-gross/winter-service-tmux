"""Tests for service_orchestrator.logwriter.

Covers:
- ``format_log_line``: RFC3339-UTC timestamp with Z suffix, tab-separated.
- ``rotate``: ladder shift, cap at max_rotations, max_rotations=0 discard.
- ``would_exceed``: size threshold predicate.
- End-to-end via subprocess: stdin→stdout echo, file timestamped lines,
  rotation by size, startup-rotate, non-UTF-8 bytes.
"""

from __future__ import annotations

import io
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from service_orchestrator.logwriter import format_log_line, rotate, run, would_exceed

# ---------------------------------------------------------------------------
# format_log_line
# ---------------------------------------------------------------------------


def test_format_log_line_z_suffix() -> None:
    """Timestamp must end with Z, not +00:00."""
    ts = datetime(2026, 6, 15, 10, 0, 1, 123456, tzinfo=UTC)
    line = format_log_line(ts, "hello")
    assert line.startswith("2026-06-15T10:00:01.123456Z\t")


def test_format_log_line_tab_separated() -> None:
    ts = datetime(2026, 6, 15, 10, 0, 0, 0, tzinfo=UTC)
    line = format_log_line(ts, "some message")
    parts = line.split("\t", 1)
    assert len(parts) == 2
    assert parts[1] == "some message\n"


def test_format_log_line_trailing_newline() -> None:
    ts = datetime(2026, 6, 15, 10, 0, 0, 0, tzinfo=UTC)
    line = format_log_line(ts, "msg")
    assert line.endswith("\n")


def test_format_log_line_empty_message() -> None:
    ts = datetime(2026, 6, 15, 10, 0, 0, 0, tzinfo=UTC)
    line = format_log_line(ts, "")
    assert "\t\n" in line


def test_format_log_line_microseconds_zero_padded() -> None:
    ts = datetime(2026, 6, 15, 10, 0, 0, 5, tzinfo=UTC)
    line = format_log_line(ts, "x")
    assert "000005Z" in line


# ---------------------------------------------------------------------------
# would_exceed
# ---------------------------------------------------------------------------


def test_would_exceed_below_threshold() -> None:
    assert would_exceed(100, 50, 200) is False


def test_would_exceed_at_threshold() -> None:
    assert would_exceed(100, 100, 200) is False


def test_would_exceed_above_threshold() -> None:
    assert would_exceed(100, 101, 200) is True


def test_would_exceed_zero_current_size() -> None:
    assert would_exceed(0, 10, 5) is True


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


def test_rotate_basic_shift(tmp_path: Path) -> None:
    """base → base.1 when max_rotations >= 1."""
    base = tmp_path / "svc.log"
    base.write_text("run1\n")
    rotate(base, max_rotations=3)
    assert not base.exists()
    assert (tmp_path / "svc.log.1").read_text() == "run1\n"


def test_rotate_shifts_existing_numbered(tmp_path: Path) -> None:
    """Existing .log.1 → .log.2 when max_rotations >= 2."""
    base = tmp_path / "svc.log"
    base.write_text("run2\n")
    (tmp_path / "svc.log.1").write_text("run1\n")
    rotate(base, max_rotations=3)
    assert (tmp_path / "svc.log.1").read_text() == "run2\n"
    assert (tmp_path / "svc.log.2").read_text() == "run1\n"


def test_rotate_drops_oldest_at_cap(tmp_path: Path) -> None:
    """Segment at max_rotations is removed; ladder shifts up."""
    base = tmp_path / "svc.log"
    base.write_text("run3\n")
    (tmp_path / "svc.log.1").write_text("run2\n")
    (tmp_path / "svc.log.2").write_text("run1\n")
    rotate(base, max_rotations=2)
    assert not (tmp_path / "svc.log").exists()
    assert (tmp_path / "svc.log.1").read_text() == "run3\n"
    assert (tmp_path / "svc.log.2").read_text() == "run2\n"
    # run1 (was .2) must be gone — capped at 2
    assert not (tmp_path / "svc.log.3").exists()


def test_rotate_max_rotations_zero_discards(tmp_path: Path) -> None:
    """max_rotations=0 removes the active file without creating .log.1."""
    base = tmp_path / "svc.log"
    base.write_text("ephemeral\n")
    rotate(base, max_rotations=0)
    assert not base.exists()
    assert not (tmp_path / "svc.log.1").exists()


def test_rotate_nonexistent_base_is_noop(tmp_path: Path) -> None:
    """Rotating a non-existent base file does not raise."""
    base = tmp_path / "svc.log"
    rotate(base, max_rotations=3)  # should not raise
    assert not base.exists()


def test_rotate_max_rotations_one(tmp_path: Path) -> None:
    """max_rotations=1: base → .1, any existing .1 is dropped."""
    base = tmp_path / "svc.log"
    base.write_text("new\n")
    (tmp_path / "svc.log.1").write_text("old\n")
    rotate(base, max_rotations=1)
    assert (tmp_path / "svc.log.1").read_text() == "new\n"
    assert not (tmp_path / "svc.log.2").exists()


# ---------------------------------------------------------------------------
# run() — in-process via injected stdin/stdout
# ---------------------------------------------------------------------------


def _make_stdin(lines: list[str]) -> io.BytesIO:
    return io.BytesIO("".join(lines).encode("utf-8"))


def test_run_echoes_raw_lines(tmp_path: Path) -> None:
    """Lines written to stdout must be verbatim raw (no timestamp prefix)."""
    logfile = tmp_path / "svc.log"
    stdin = _make_stdin(["hello\n", "world\n"])
    out = io.StringIO()
    rc = run(logfile, rotate_size=1_000_000, max_rotations=3, stdin=stdin, stdout=out)
    assert rc == 0
    assert out.getvalue() == "hello\nworld\n"


def test_run_writes_timestamped_lines(tmp_path: Path) -> None:
    """Each log file line must be <RFC3339-Z-ts>\\t<msg>\\n."""
    logfile = tmp_path / "svc.log"
    stdin = _make_stdin(["alpha\n", "beta\n"])
    out = io.StringIO()
    run(logfile, rotate_size=1_000_000, max_rotations=3, stdin=stdin, stdout=out)
    lines = logfile.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        ts_part, _, msg_part = line.partition("\t")
        assert ts_part.endswith("Z"), f"timestamp does not end with Z: {ts_part!r}"
        assert msg_part in ("alpha", "beta")


def test_run_startup_rotates_existing_file(tmp_path: Path) -> None:
    """Non-empty active file at startup → rotated to .log.1 before first write."""
    logfile = tmp_path / "svc.log"
    logfile.write_text("previous run\n")
    stdin = _make_stdin(["new line\n"])
    out = io.StringIO()
    run(logfile, rotate_size=1_000_000, max_rotations=3, stdin=stdin, stdout=out)
    assert (tmp_path / "svc.log.1").read_text() == "previous run\n"
    content = logfile.read_text()
    assert "new line" in content
    assert "previous run" not in content


def test_run_no_startup_rotate_when_empty(tmp_path: Path) -> None:
    """Empty active file at startup → no .log.1 created."""
    logfile = tmp_path / "svc.log"
    logfile.write_text("")  # empty
    stdin = _make_stdin(["msg\n"])
    out = io.StringIO()
    run(logfile, rotate_size=1_000_000, max_rotations=3, stdin=stdin, stdout=out)
    assert not (tmp_path / "svc.log.1").exists()


def test_run_creates_parent_dir(tmp_path: Path) -> None:
    """Parent directory is created if absent."""
    logfile = tmp_path / "nested" / "deep" / "svc.log"
    stdin = _make_stdin(["msg\n"])
    out = io.StringIO()
    run(logfile, rotate_size=1_000_000, max_rotations=3, stdin=stdin, stdout=out)
    assert logfile.exists()


def test_run_rotates_by_size(tmp_path: Path) -> None:
    """Writing past rotate_size triggers rotation within the run loop."""
    logfile = tmp_path / "svc.log"
    # Each line ~20 bytes; rotate after 30 bytes → rotation after 2nd line.
    lines = [f"line{i:02d}\n" for i in range(5)]
    stdin = _make_stdin(lines)
    out = io.StringIO()
    run(logfile, rotate_size=30, max_rotations=3, stdin=stdin, stdout=out)
    # Active file must exist and at least one rotated segment must exist.
    assert logfile.exists()
    assert (tmp_path / "svc.log.1").exists()


# ---------------------------------------------------------------------------
# End-to-end via subprocess
# ---------------------------------------------------------------------------

_LOGWRITER = Path(__file__).parent.parent / "src" / "service_orchestrator" / "logwriter.py"


def _run_writer(
    tmp_path: Path,
    lines: list[str],
    rotate_size: int = 1_000_000,
    max_rotations: int = 3,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    import os

    logfile = tmp_path / "svc.log"
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [
            sys.executable,
            str(_LOGWRITER),
            str(logfile),
            "--rotate-size",
            str(rotate_size),
            "--max-rotations",
            str(max_rotations),
        ],
        input="".join(lines),
        capture_output=True,
        text=True,
        env=env,
    )


def test_subprocess_echo_and_file(tmp_path: Path) -> None:
    """Subprocess: stdout echoes raw lines; file has RFC3339-Z-ts lines."""
    result = _run_writer(tmp_path, ["hello\n", "world\n"])
    assert result.returncode == 0
    # stdout = raw echo
    assert result.stdout == "hello\nworld\n"
    # file = timestamped lines
    logfile = tmp_path / "svc.log"
    lines = logfile.read_text().splitlines()
    assert len(lines) == 2
    for raw, file_line in zip(["hello", "world"], lines, strict=True):
        ts, _, msg = file_line.partition("\t")
        assert ts.endswith("Z")
        assert msg == raw


def test_subprocess_startup_rotate(tmp_path: Path) -> None:
    """Second subprocess invocation startup-rotates previous run to .log.1."""
    logfile = tmp_path / "svc.log"
    # First run.
    _run_writer(tmp_path, ["first run\n"])
    first_content = logfile.read_text()
    # Second run.
    _run_writer(tmp_path, ["second run\n"])
    assert (tmp_path / "svc.log.1").read_text() == first_content
    assert "second run" in logfile.read_text()
    assert "first run" not in logfile.read_text()


def test_subprocess_rotation_by_size_caps(tmp_path: Path) -> None:
    """Rotation by size keeps at most max_rotations numbered segments."""
    # Use a tiny rotate_size and max_rotations=2.
    lines = [f"{'x' * 40}\n" for _ in range(10)]
    _run_writer(tmp_path, lines, rotate_size=50, max_rotations=2)
    tmp_files = list(tmp_path.iterdir())
    segments = [f for f in tmp_files if f.name.startswith("svc.log")]
    # Must have .log + at most .log.1 + .log.2
    assert (tmp_path / "svc.log").exists()
    for seg in segments:
        if seg.suffix.lstrip(".").isdigit():
            n = int(seg.suffix.lstrip("."))
            assert n <= 2, f"segment {seg.name} exceeds max_rotations=2"


def test_subprocess_non_utf8_does_not_crash(tmp_path: Path) -> None:
    """Non-UTF-8 bytes on stdin must not crash the writer."""
    logfile = tmp_path / "svc.log"
    import os

    bad_bytes = b"normal line\n\xff\xfe bad bytes\nnormal again\n"
    result = subprocess.run(
        [
            sys.executable,
            str(_LOGWRITER),
            str(logfile),
            "--rotate-size",
            "1000000",
            "--max-rotations",
            "3",
        ],
        input=bad_bytes,
        capture_output=True,
        env=os.environ,
    )
    assert result.returncode == 0
    # Active log must contain entries for all three input lines.
    log_text = logfile.read_text(encoding="utf-8", errors="replace")
    assert log_text.count("\n") == 3
