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

    def term_then_kill(self, pids: list[int]) -> None:
        """SIGTERM all *pids*, wait 1 s, then SIGKILL survivors.

        Mirrors the bash pattern in ``workflow/down``::

            kill -TERM "${ALL_PIDS[@]}" 2>/dev/null || true
            sleep 1
            kill -9  "${ALL_PIDS[@]}" 2>/dev/null || true

        Individual signal errors are suppressed — a PID may have already exited
        between collection and the kill call.
        """
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                pass
        time.sleep(1)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass


def _conforms_pgrep_process_reaper(x: PgrepProcessReaper) -> IProcessReaper:
    return x
