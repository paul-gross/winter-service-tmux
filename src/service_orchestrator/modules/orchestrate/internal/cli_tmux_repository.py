"""``tmux`` CLI adapter.  All subprocess calls to tmux are confined here.

Matches the exact ``tmux`` invocations used by the bash scripts:
- ``has_session``    → ``tmux has-session -t <session>``
- ``list_sessions``  → ``tmux list-sessions -F '#{session_name}'``
- ``new_session``    → ``tmux new-session -d -s <session> -c <cwd> -x <w> -y <h>``
- ``kill_session``   → ``tmux kill-session -t <session>``
- ``list_windows``   → ``tmux list-windows -t <session> -F '#{window_index}'``
- ``list_panes``     → ``tmux list-panes -s -t <session> -F '#{window_index}.#{pane_index} #{pane_pid}'``
- ``send_keys``      → ``tmux send-keys -t <session>:<target> <line> Enter``
- ``capture_pane``   → ``tmux capture-pane -t <session>:<target> -p``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from service_orchestrator.modules.orchestrate.internal.tmux_error_factory import TmuxErrorFactory
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository, PaneInfo


class CliTmuxRepository:
    """Subprocess adapter for ``ITmuxRepository``.  All tmux I/O is confined here."""

    def __init__(self, error_factory: TmuxErrorFactory | None = None) -> None:
        self._errors = error_factory or TmuxErrorFactory()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def has_session(self, session: str) -> bool:
        """Return ``True`` when the named tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def list_sessions(self) -> list[str]:
        """Return the names of all running tmux sessions."""
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # No server or no sessions — treat as empty rather than error.
            return []
        return [line for line in result.stdout.splitlines() if line]

    def new_session(self, session: str, cwd: Path, width: int, height: int) -> None:
        """Create a new detached tmux session."""
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", str(cwd), "-x", str(width), "-y", str(height)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise self._errors.from_subprocess(result, f"new-session '{session}' failed", cwd=cwd)

    def kill_session(self, session: str) -> None:
        """Kill a tmux session (non-fatal when it no longer exists)."""
        result = subprocess.run(
            ["tmux", "kill-session", "-t", session],
            capture_output=True,
            text=True,
            check=False,
        )
        # Mirrors `tmux kill-session ... || true` in the bash scripts.
        # We only raise on unexpected errors (returncode > 1).
        if result.returncode not in (0, 1):
            raise self._errors.from_subprocess(result, f"kill-session '{session}' failed")

    # ------------------------------------------------------------------
    # Window / pane inspection
    # ------------------------------------------------------------------

    def list_windows(self, session: str) -> list[str]:
        """Return window index strings for all windows in *session*."""
        result = subprocess.run(
            ["tmux", "list-windows", "-t", session, "-F", "#{window_index}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise self._errors.from_subprocess(result, f"list-windows '{session}' failed")
        return [line for line in result.stdout.splitlines() if line]

    def list_panes(self, session: str) -> list[PaneInfo]:
        """Return all panes across all windows in *session*.

        Uses ``-s`` (session-wide) so the output covers multi-window layouts.
        Each line is ``<window_index>.<pane_index> <pane_pid>`` — mirrors the
        bash ``tmux list-panes -s -t "$SESSION" -F '#{window_index}.#{pane_index} #{pane_pid}'``.
        """
        result = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", session, "-F", "#{window_index}.#{pane_index} #{pane_pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise self._errors.from_subprocess(result, f"list-panes '{session}' failed")
        panes: list[PaneInfo] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            target, pid_str = parts
            try:
                panes.append(PaneInfo(target=target, pid=int(pid_str)))
            except ValueError:
                continue
        return panes

    # ------------------------------------------------------------------
    # Pane interaction
    # ------------------------------------------------------------------

    def send_keys(self, session: str, target: str, line: str) -> None:
        """Send *line* followed by Enter to the pane at *session*:*target*."""
        result = subprocess.run(
            ["tmux", "send-keys", "-t", f"{session}:{target}", line, "Enter"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise self._errors.from_subprocess(result, f"send-keys to '{session}:{target}' failed")

    def capture_pane(self, session: str, target: str) -> str:
        """Return the visible content of the pane at *session*:*target*.

        Mirrors ``tmux capture-pane -t "$SESSION:$PANE_TARGET" -p`` in the
        bash status script.
        """
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", f"{session}:{target}", "-p"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise self._errors.from_subprocess(result, f"capture-pane '{session}:{target}' failed")
        return result.stdout


def _conforms_cli_tmux_repository(x: CliTmuxRepository) -> ITmuxRepository:
    return x
