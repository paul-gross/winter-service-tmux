"""Process reaping seam.

``IProcessReaper`` abstracts the ``pgrep``/``kill``/``ps`` operations used by
``down`` (whole session), ``restart`` (single-pane children), and the
``uptime`` health probe (elapsed time of a pane's child).  The adapter lives
in ``internal/pgrep_process_reaper.py``.
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

    def child_uptime_seconds(self, pid: int) -> int | None:
        """Return the uptime, in seconds, of *pid*'s longest-running direct child.

        Uses ``pgrep -P <pid>`` to find direct children (the same call
        ``has_children`` makes), then reads each child's elapsed time via
        ``ps -o etimes=``. When several direct children exist, returns the
        largest elapsed time (the longest-running one) rather than any
        specific service PID — this is deliberate, not an approximation: a
        FILE-mode captured service's pane has TWO direct children (the
        service's own command and the capture writer that pipes its
        output), and the contract tracks whichever of them has been alive
        the longest, not a specifically-identified "the service" PID.
        Returns ``None`` when *pid* has no direct children (an
        interactive/empty-command pane, or the measured process already
        exited) — the caller (an ``uptime`` health probe) treats ``None`` as
        unhealthy.
        """
        ...
