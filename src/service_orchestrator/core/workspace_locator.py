"""Cross-cutting workspace location seam.

``IWorkspaceLocator`` is hoisted into ``core/`` because it is used by both the
orchestrate module (to locate the worktree dir per env) and the CLI doors (to
resolve the workspace root when ``WINTER_WORKSPACE_DIR`` is absent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class IWorkspaceLocator(Protocol):
    """Resolve workspace and per-env worktree paths."""

    def workspace_root(self) -> Path: ...
    def worktree_dir(self, env: str) -> Path: ...
