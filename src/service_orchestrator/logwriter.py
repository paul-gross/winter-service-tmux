"""Capture writer — tees service stdout/stderr to a timestamped log file.

Invoked by absolute path as a standalone script (no PYTHONPATH required). The
capture pipe in ``status_report.py`` spawns it under ``sys.executable`` (the
orchestrator's own interpreter), not a bare ``python3``, so the writer always
matches the Python version the orchestrator is running::

    <sys.executable> /abs/.../logwriter.py <logfile> --rotate-size <bytes> --max-rotations <count>

Reads stdin line-by-line.  For each line:

1. Echoes the raw line verbatim to stdout (keeps the tmux pane live).
2. Appends ``<RFC3339-UTC-ts>\\t<raw line>\\n`` to the active log file.
3. Rotates the active file when its size would exceed ``--rotate-size``.

On startup, if the active log file is non-empty, it is rotated so that each
``up`` begins a fresh segment with no interleaving of runs.

This module imports nothing outside the standard library and nothing from the
``service_orchestrator`` or ``service_manifest`` packages.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

# ---------------------------------------------------------------------------
# Pure helpers (importable and unit-testable without I/O setup)
# ---------------------------------------------------------------------------


def format_log_line(ts: datetime, message: str) -> str:
    """Format one persisted log line from a timestamp and raw message text.

    Returns ``<RFC3339-UTC-ts>\\t<message>\\n`` with a trailing newline.
    The timestamp is rendered in RFC3339 UTC with microseconds, ending in
    ``Z`` (e.g. ``2026-06-15T10:00:01.123456Z``).

    *message* must be the raw line text with its trailing newline already
    stripped by the caller.
    """
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond:06d}Z"
    return f"{ts_str}\t{message}\n"


def would_exceed(current_size: int, line_bytes: int, rotate_size: int) -> bool:
    """Return True if writing *line_bytes* would push the file past *rotate_size*."""
    return (current_size + line_bytes) > rotate_size


def rotate(base: Path, max_rotations: int) -> None:
    """Rotate *base* log file, keeping at most *max_rotations* numbered segments.

    When *max_rotations* is 0, the active file is discarded (truncated/removed)
    with no backup segments kept.

    Otherwise, the rotation ladder is:

    - Remove ``base.<max_rotations>`` if it exists (oldest segment dropped).
    - Shift ``base.<n>`` → ``base.<n+1>`` for n from ``max_rotations - 1`` down to 1.
    - Move ``base`` → ``base.1``.

    After this call the active *base* file no longer exists; the next write
    creates a fresh empty file.
    """
    if max_rotations == 0:
        # Discard — no backups.
        with contextlib.suppress(FileNotFoundError):
            base.unlink()
        return

    # Drop the oldest segment beyond the cap.
    oldest = Path(f"{base}.{max_rotations}")
    with contextlib.suppress(FileNotFoundError):
        oldest.unlink()

    # Shift existing numbered segments up.
    for n in range(max_rotations - 1, 0, -1):
        src = Path(f"{base}.{n}")
        dst = Path(f"{base}.{n + 1}")
        if src.exists():
            src.rename(dst)

    # Move the active file to .1.
    if base.exists():
        base.rename(Path(f"{base}.1"))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _open_log(path: Path) -> IO[bytes]:
    """Open *path* for append in binary mode (returns a binary file object)."""
    return open(path, "ab")


def run(
    logfile: Path,
    rotate_size: int,
    max_rotations: int,
    stdin: IO[bytes],
    stdout: IO[str],
) -> int:
    """Core writer loop.  Returns an integer exit code (0 on clean exit).

    Separated from ``main`` so it can be exercised in tests with injected
    stdin/stdout.  Real callers pass ``sys.stdin.buffer`` and ``sys.stdout``.
    """
    # Ensure the parent directory exists.
    logfile.parent.mkdir(parents=True, exist_ok=True)

    # Startup rotate: if the active file is non-empty, rotate it so this run
    # begins a fresh segment.
    if logfile.exists() and logfile.stat().st_size > 0:
        rotate(logfile, max_rotations)

    log_fh = _open_log(logfile)
    current_size = 0

    try:
        for raw_bytes in stdin:
            # Decode with error replacement so stray control/non-UTF-8 bytes
            # never crash the writer.
            raw_line = raw_bytes.decode("utf-8", errors="replace")

            # Echo raw line verbatim to pane stdout (no timestamp prefix).
            stdout.write(raw_line)
            stdout.flush()

            # Build the timestamped log entry.
            ts = datetime.now(UTC)
            message = raw_line.rstrip("\n")
            entry = format_log_line(ts, message)
            entry_bytes = entry.encode("utf-8", errors="replace")

            # Pre-write rotation check.
            if current_size > 0 and would_exceed(current_size, len(entry_bytes), rotate_size):
                log_fh.flush()
                log_fh.close()
                rotate(logfile, max_rotations)
                log_fh = _open_log(logfile)
                current_size = 0

            log_fh.write(entry_bytes)
            log_fh.flush()
            current_size += len(entry_bytes)

            # Post-write rotation check (handles the case where a single line
            # is itself larger than rotate_size).
            if would_exceed(current_size, 0, rotate_size):
                log_fh.flush()
                log_fh.close()
                rotate(logfile, max_rotations)
                log_fh = _open_log(logfile)
                current_size = 0

    finally:
        log_fh.flush()
        log_fh.close()

    return 0


def main(argv: list[str]) -> int:
    """Parse CLI arguments and run the capture writer.

    Returns an integer exit code.
    """
    parser = argparse.ArgumentParser(
        description="Capture writer: tee stdin to a timestamped log file.",
        prog="logwriter",
    )
    parser.add_argument(
        "logfile",
        type=Path,
        help="Absolute path to the active log file.",
    )
    parser.add_argument(
        "--rotate-size",
        type=int,
        default=10 * 1024 * 1024,  # 10 MiB default
        metavar="BYTES",
        help="Rotate the active file when it would exceed this many bytes.",
    )
    parser.add_argument(
        "--max-rotations",
        type=int,
        default=5,
        metavar="COUNT",
        help="Number of rotated segments to keep (.log.1 … .log.N).",
    )
    args = parser.parse_args(argv)

    # Install clean-exit signal handlers.
    # Exiting 0 on SIGINT/SIGTERM is deliberate: the writer is a downstream
    # pipe process, not the consumer, so it should flush and exit cleanly when
    # the service pane closes.  This does not conflict with the ``logs``
    # consumer's exit-130-on-SIGINT convention — that convention applies to the
    # reader process, not the writer.
    def _handle_signal(signum: int, frame: object) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    return run(
        logfile=args.logfile,
        rotate_size=args.rotate_size,
        max_rotations=args.max_rotations,
        stdin=sys.stdin.buffer,
        stdout=sys.stdout,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
