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

    def reap_descendants(self, root_pids: list[int]) -> None:
        """SIGTERM all descendants of *root_pids*, sleep 1 s, re-collect, then SIGKILL.

        Re-collecting after the sleep catches grandchildren forked during the
        shutdown window (between the initial snapshot and their parent's death).
        *root_pids* are the pane shell PIDs; they are never signalled themselves.
        """
        ...
