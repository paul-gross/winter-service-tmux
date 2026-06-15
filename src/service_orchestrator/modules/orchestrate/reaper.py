"""Process reaping seam.

``IProcessReaper`` abstracts the ``pgrep``/``kill`` operations used by
``down`` (whole session) and ``restart`` (single-pane children).  The adapter
lives in ``internal/pgrep_process_reaper.py``.
"""

from __future__ import annotations

from typing import Protocol


class IProcessReaper(Protocol):
    """Collect and terminate descendant processes via pgrep/kill."""

    def descendants(self, pid: int) -> list[int]:
        """Return all recursive descendant PIDs of *pid*, excluding *pid* itself.

        Uses repeated ``pgrep -P`` walks.  Returns an empty list when *pid* has
        no children or does not exist.
        """
        ...

    def has_children(self, pid: int) -> bool:
        """Return ``True`` when *pid* has at least one direct child process."""
        ...

    def term_then_kill(self, pids: list[int]) -> None:
        """Send SIGTERM to all *pids*, sleep 1 s, then SIGKILL survivors.

        Mirrors the bash TERM/sleep/KILL pattern in ``workflow/down`` and
        ``workflow/restart``.  Errors from individual kill calls are suppressed
        (some PIDs may already have exited).
        """
        ...
