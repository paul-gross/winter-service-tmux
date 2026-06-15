"""Tmux subprocess seam and its associated domain type.

``ITmuxRepository`` is the single point of contact for all ``tmux`` CLI
invocations.  All subprocess calls live in ``internal/cli_tmux_repository.py``;
services and tests depend on this Protocol only.

``PaneInfo`` is a frozen dataclass returned by ``list_panes`` — callers pattern-
match on ``target`` to find a specific pane and use ``pid`` for reaping.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PaneInfo:
    """Snapshot of one tmux pane returned by ``ITmuxRepository.list_panes``."""

    target: str  # "<window_index>.<pane_index>"
    pid: int  # pane_pid (the pane's login shell)


class ITmuxRepository(Protocol):
    """All tmux CLI operations.  Subprocess calls are confined to the adapter."""

    def has_session(self, session: str) -> bool: ...

    def list_sessions(self) -> list[str]: ...

    def new_session(
        self,
        session: str,
        cwd: Path,
        width: int,
        height: int,
    ) -> None: ...

    def kill_session(self, session: str) -> None: ...

    def list_windows(self, session: str) -> list[str]: ...

    def list_panes(self, session: str) -> list[PaneInfo]: ...

    def send_keys(self, session: str, target: str, line: str) -> None: ...

    def capture_pane(self, session: str, target: str) -> str: ...
