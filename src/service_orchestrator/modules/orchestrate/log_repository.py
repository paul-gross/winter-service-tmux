"""Log-file path and directory seam.

``ILogRepository`` is the single point of contact for log-directory and
log-path resolution plus directory creation.  All filesystem I/O for log
paths lives in ``internal/local_log_repository.py``; services and tests
depend on this Protocol only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ILogRepository(Protocol):
    """Log-directory and log-path operations.  Filesystem I/O is confined to the adapter."""

    def log_dir(self, worktree_dir: Path) -> Path:
        """Return the log directory path for *worktree_dir*.

        Returns ``<worktree_dir>/.winter/logs``.  The directory is not
        created — use ``ensure_log_dir`` when creation is needed.
        """
        ...

    def ensure_log_dir(self, worktree_dir: Path) -> Path:
        """Create the log directory if absent and return its path.

        Equivalent to ``mkdir -p <worktree_dir>/.winter/logs``.  Idempotent.
        """
        ...

    def log_path(self, worktree_dir: Path, service: str) -> Path:
        """Return the active log-file path for *service* under *worktree_dir*.

        Returns ``<log_dir>/<service>.log`` where ``<log_dir>`` is the value
        of ``log_dir(worktree_dir)``.
        """
        ...

    def segment_files(self, worktree_dir: Path, service: str) -> list[Path]:
        """Return existing log segment files for *service* in chronological order (oldest first).

        Rotation produces ``<svc>.log``, ``<svc>.log.1`` … ``<svc>.log.<N>``
        where ``.log`` is newest and higher numbers are older.  This method
        returns ``[<svc>.log.<N>, …, <svc>.log.1, <svc>.log]``, omitting any
        segments that do not exist on disk.
        """
        ...

    def read_lines(self, path: Path) -> list[str]:
        """Read all lines from *path*, newline-stripped.

        Decodes with ``errors="replace"``.  Returns an empty list when the
        file is absent.
        """
        ...

    def file_size(self, path: Path) -> int:
        """Return the current byte size of *path*, or 0 if the file is absent."""
        ...

    def read_new_lines(self, path: Path, offset: int) -> tuple[list[str], int]:
        """Read lines appended to *path* since *offset* bytes.

        Returns ``(lines, new_offset)`` where *new_offset* is the byte
        position after the last complete line read (ready for the next call).
        Lines are newline-stripped.  Returns ``([], offset)`` when nothing new
        has been written or the file is absent.

        If the file is smaller than *offset* (truncated / rotated), the caller
        should reset *offset* to 0 and call again — this method does NOT
        handle that; it returns ``([], offset)`` when ``file_size < offset``.
        """
        ...

    def rotated_segments(self, worktree_dir: Path, service: str) -> list[Path]:
        """Return all rotated segment files for *service* under *worktree_dir*.

        A rotated segment is any file matching ``<service>.log.<N>`` where N is
        a positive integer.  The active log file (``<service>.log``) is never
        included.  Segments beyond ``max_rotations`` that remain on disk are
        still returned so they can be pruned.
        """
        ...

    def mtime(self, path: Path) -> float:
        """Return the modification time of *path* in seconds since the epoch.

        Uses ``os.stat`` / ``pathlib.stat``.  The return value is suitable for
        comparison with a ``time.time()``-derived cutoff.
        """
        ...

    def delete(self, path: Path) -> None:
        """Remove *path* from the filesystem.

        No-op (does not raise) when *path* does not exist.
        """
        ...
