"""Adapter that resolves the workspace root from the environment or a marker walk.

Resolution order:
1. ``WINTER_WORKSPACE_DIR`` environment variable (authoritative when set).
2. Walk up from *start_dir* (defaults to ``cwd``) looking for ``.winter/config.toml``.

``worktree_dir(env)`` returns ``workspace_root() / env``.
"""

from __future__ import annotations

import os
from pathlib import Path

from service_orchestrator.core.workspace_locator import IWorkspaceLocator

_MARKER = Path(".winter") / "config.toml"


class EnvWorkspaceLocator:
    """Resolve workspace root via WINTER_WORKSPACE_DIR or marker-walk.

    Accepts an optional *start_dir* for the marker-walk fallback; defaults to
    the process cwd.  Constructed once at startup; the resolved root is cached
    after the first call to ``workspace_root()``.
    """

    def __init__(self, start_dir: Path | None = None) -> None:
        self._start_dir = start_dir
        self._cached: Path | None = None

    def workspace_root(self) -> Path:
        if self._cached is not None:
            return self._cached

        env_val = os.environ.get("WINTER_WORKSPACE_DIR")
        if env_val:
            self._cached = Path(env_val)
            return self._cached

        start = self._start_dir if self._start_dir is not None else Path.cwd()
        candidate = start
        while True:
            if (candidate / _MARKER).exists():
                self._cached = candidate
                return self._cached
            parent = candidate.parent
            if parent == candidate:
                raise RuntimeError(
                    f"workspace root not found: no {_MARKER} found walking up from {start}; "
                    "set WINTER_WORKSPACE_DIR to point at the workspace root"
                )
            candidate = parent

    def worktree_dir(self, env: str) -> Path:
        return self.workspace_root() / env


def _conforms_env_workspace_locator(x: EnvWorkspaceLocator) -> IWorkspaceLocator:
    return x
