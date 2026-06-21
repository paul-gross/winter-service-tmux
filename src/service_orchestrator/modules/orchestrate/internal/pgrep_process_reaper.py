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

import logging
import os
import signal
import subprocess
import time

from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.reaper import IProcessReaper

logger = logging.getLogger(__name__)


class PgrepProcessReaper:
    """``pgrep``/``kill`` adapter implementing ``IProcessReaper``.

    All ``pgrep`` and ``kill`` subprocess calls are confined here.
    """

    def _run_pgrep(self, pid: int) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        """Run ``pgrep -P <pid>`` and return the result.

        Raises :class:`OrchestratorError` if ``pgrep`` is not on ``PATH``.
        """
        try:
            return subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise OrchestratorError(f"pgrep not found; cannot collect descendants: {exc}") from exc

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
        result = self._run_pgrep(pid)
        # pgrep exits 1 when no children exist — not an error.
        children = [int(line) for line in result.stdout.splitlines() if line.strip()]
        for child in children:
            self._collect(child, root=root, out=out)
        if pid != root:
            out.append(pid)

    def has_children(self, pid: int) -> bool:
        """Return ``True`` when *pid* has at least one direct child process."""
        result = self._run_pgrep(pid)
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
                # Process already exited — not an error; ignore.
                pass
            except OSError as exc:
                # Non-ESRCH failure (e.g. EPERM — process re-parented after
                # collection; cannot signal it).  Emit a warning so a stuck
                # process is visible instead of silently surviving.
                logger.warning("reaper: SIGTERM pid %d failed: %s", pid, exc)

        time.sleep(1)

        # Re-collect after the sleep to catch grandchildren forked during shutdown.
        second_pass: list[int] = []
        for root in root_pids:
            second_pass.extend(self.descendants(root))

        for pid in second_pass:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                # Process already exited — not an error; ignore.
                pass
            except OSError as exc:
                # Non-ESRCH failure (e.g. EPERM).  Warn so the stuck process
                # is visible.
                logger.warning("reaper: SIGKILL pid %d failed: %s", pid, exc)


def _conforms_pgrep_process_reaper(x: PgrepProcessReaper) -> IProcessReaper:
    return x
