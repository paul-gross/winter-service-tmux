"""Local filesystem adapter for ``ILogRepository``.

All ``pathlib`` I/O for log-directory creation and log-path construction is
confined here.  ``OrchestratorService`` and other services depend on
``ILogRepository`` only.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from service_orchestrator.modules.orchestrate.log_repository import ILogRepository


class LocalLogRepository:
    """``pathlib`` adapter implementing ``ILogRepository``.

    Log files live at ``<worktree_dir>/.winter/logs/<service>.log``.
    """

    def log_dir(self, worktree_dir: Path) -> Path:
        """Return ``<worktree_dir>/.winter/logs`` without creating it."""
        return worktree_dir / ".winter" / "logs"

    def ensure_log_dir(self, worktree_dir: Path) -> Path:
        """Create ``<worktree_dir>/.winter/logs`` (and parents) if absent.

        Idempotent: does nothing when the directory already exists.
        Returns the directory path.
        """
        log_dir = self.log_dir(worktree_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def log_path(self, worktree_dir: Path, service: str) -> Path:
        """Return ``<log_dir>/<service>.log`` for *service* under *worktree_dir*."""
        return self.log_dir(worktree_dir) / f"{service}.log"

    def segment_files(self, worktree_dir: Path, service: str) -> list[Path]:
        """Return existing log segment files for *service* in chronological order (oldest first).

        Checks for ``<svc>.log.N`` (highest N = oldest) down to ``<svc>.log.1``,
        then ``<svc>.log`` (newest), returning only files that exist.
        """
        base = self.log_path(worktree_dir, service)
        segments: list[Path] = []

        # Collect numbered segments in reverse-number order (highest = oldest).
        # We don't know max_rotations here, so we probe until we find a gap.
        n = 1
        numbered: list[Path] = []
        while True:
            candidate = Path(f"{base}.{n}")
            if candidate.exists():
                numbered.append(candidate)
                n += 1
            else:
                break

        # Oldest first: highest number first.
        segments.extend(reversed(numbered))

        # Append the active file if it exists.
        if base.exists():
            segments.append(base)

        return segments

    def read_lines(self, path: Path) -> list[str]:
        """Read all lines from *path*, newline-stripped.

        Returns an empty list when the file is absent.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        return [line.rstrip("\n") for line in text.splitlines()]

    def read_tail(self, path: Path, max_bytes: int) -> str:
        """Return up to the last *max_bytes* bytes of *path*, decoded as text.

        Seeks near the end of the file instead of reading it in full.
        Returns ``""`` when *path* is absent.
        """
        try:
            with path.open("rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, size - max_bytes))
                chunk = fh.read()
        except FileNotFoundError:
            return ""
        return chunk.decode("utf-8", errors="replace")

    def file_size(self, path: Path) -> int:
        """Return the current byte size of *path*, or 0 if absent."""
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def read_new_lines(self, path: Path, offset: int) -> tuple[list[str], int]:
        """Read lines appended to *path* since *offset* bytes.

        Opens the file in binary mode, seeks to *offset*, reads until EOF,
        and returns complete (newline-terminated) lines only — any trailing
        partial line is left for the next poll tick.

        Returns ``([], offset)`` when the file is absent or when no complete
        new lines have been written since *offset*.
        """
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except FileNotFoundError:
            return [], offset

        if not chunk:
            return [], offset

        # Track the offset in the byte domain: find the last newline byte in
        # the raw chunk, advance by that many bytes, then decode only the
        # complete-bytes slice.  The decoded string never feeds back into
        # offset arithmetic — re-encoding would corrupt the offset because
        # U+FFFD (the replacement char for invalid bytes) encodes to 3 bytes
        # while the original invalid byte was 1.
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            # No complete line yet.
            return [], offset

        complete_bytes = chunk[: last_nl + 1]
        consumed = len(complete_bytes)
        text = complete_bytes.decode("utf-8", errors="replace")
        lines = [line.rstrip("\n") for line in text.splitlines() if line.rstrip("\n")]
        return lines, offset + consumed

    def rotated_segments(self, worktree_dir: Path, service: str) -> list[Path]:
        """Return all rotated segment files for *service* under *worktree_dir*.

        Globs ``<service>.log.*`` and filters to files whose suffix after the
        last ``.`` is all digits — i.e. ``<service>.log.<N>`` where N is a
        positive integer.  Non-numeric suffixes (e.g. ``backend.log.bak``) are
        excluded so operator backup files are never pruned.  The active
        ``<service>.log`` (no numeric suffix) is never included.
        """
        log_dir = self.log_dir(worktree_dir)
        pattern = f"{service}.log.*"
        return sorted(p for p in log_dir.glob(pattern) if p.suffix.lstrip(".").isdigit())

    def mtime(self, path: Path) -> float:
        """Return the modification time of *path* in seconds since the epoch."""
        return os.stat(path).st_mtime

    def delete(self, path: Path) -> None:
        """Remove *path*, silently ignoring ``FileNotFoundError``."""
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _conforms_local_log_repository(x: LocalLogRepository) -> ILogRepository:
    return x
