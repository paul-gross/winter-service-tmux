"""``pgrep``/``kill`` adapter.  All process-reaping subprocess calls are confined here.

Ports the recursive descendant collection and TERM→KILL sweep from:
- ``workflow/down``    — whole-session reap
- ``workflow/restart`` — single-pane child reap

The bash ``collect_descendants`` function walks ``pgrep -P <pid>`` recursively,
excluding the root PID from the collected set (for ``down``) or excluding the
pane shell PID (for ``restart``).  This adapter unifies both: ``descendants``
always excludes the root *pid* and returns all others.  Callers decide which
root to pass.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

from service_orchestrator.modules.orchestrate.reaper import IProcessReaper


class PgrepProcessReaper:
    """``pgrep``/``kill`` adapter implementing ``IProcessReaper``.

    All ``pgrep`` and ``kill`` subprocess calls are confined here.
    """

    def descendants(self, pid: int) -> list[int]:
        """Return all recursive descendant PIDs of *pid*, excluding *pid* itself.

        Mirrors the bash ``collect_descendants`` function: calls ``pgrep -P
        <pid>`` to get direct children, then recurses into each child.  The
        root *pid* is never included in the returned list (so callers can safely
        reap all descendants while the root — e.g. a pane shell — survives).
        """
        collected: list[int] = []
        self._collect(pid, root=pid, out=collected)
        return collected

    def _collect(self, pid: int, *, root: int, out: list[int]) -> None:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        # pgrep exits 1 when no children exist — not an error.
        children = [int(line) for line in result.stdout.splitlines() if line.strip()]
        for child in children:
            self._collect(child, root=root, out=out)
        if pid != root:
            out.append(pid)

    def has_children(self, pid: int) -> bool:
        """Return ``True`` when *pid* has at least one direct child process."""
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def reap_descendants(self, root_pids: list[int]) -> None:
        """SIGTERM all descendants of *root_pids*, sleep 1 s, re-collect, SIGKILL.

        Re-collecting after the sleep catches grandchildren forked during the
        shutdown window (between the initial snapshot and their parent's death).
        *root_pids* are the pane shell PIDs; they are never signalled themselves.
        """
        if not root_pids:
            return

        # Initial collection and TERM.
        first_pass: list[int] = []
        for root in root_pids:
            first_pass.extend(self.descendants(root))

        for pid in first_pass:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                pass

        time.sleep(1)

        # Re-collect after the sleep to catch grandchildren forked during shutdown.
        second_pass: list[int] = []
        for root in root_pids:
            second_pass.extend(self.descendants(root))

        for pid in second_pass:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass


def _conforms_pgrep_process_reaper(x: PgrepProcessReaper) -> IProcessReaper:
    return x
