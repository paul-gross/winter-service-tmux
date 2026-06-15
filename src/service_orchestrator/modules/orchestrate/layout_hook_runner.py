"""Layout hook runner seam.

``ILayoutHookRunner`` abstracts executing the optional bash layout hook
declared in ``setup-tmux.toml`` (``layout_hook`` field).  The adapter lives in
``internal/subprocess_layout_hook_runner.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ILayoutHookRunner(Protocol):
    """Run an optional bash layout hook inside a tmux session."""

    def run(
        self,
        hook_path: Path,
        env: dict[str, str],
        cwd: Path,
    ) -> None:
        """Execute the hook script at *hook_path*.

        *env* is the process environment to pass to the hook (typically
        ``os.environ`` extended with ``WINTER_SESSION``, ``WINTER_ENV``, etc.).
        *cwd* is the working directory the hook runs in (typically the worktree
        root).

        Raises ``OrchestratorError`` on non-zero exit.
        """
        ...
