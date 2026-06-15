"""Bash layout hook runner adapter.  Subprocess call confined here.

Runs the optional bash hook script declared as ``layout_hook`` in
``setup-tmux.toml``.  The hook is given the environment vars and cwd
supplied by the caller (typically the orchestrator service, which sets
``WINTER_TMUX_SESSION``, ``WINTER_TMUX_WORKTREE_DIR``, ``WINTER_ENV``, etc.).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner


class SubprocessLayoutHookRunner:
    """Run a bash layout hook script in a subprocess.

    All subprocess calls are confined here.
    """

    def run(self, hook_path: Path, env: dict[str, str], cwd: Path) -> None:
        """Execute *hook_path* with *env* as its environment.

        Raises ``OrchestratorError`` on non-zero exit.
        """
        result = subprocess.run(
            [str(hook_path)],
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise OrchestratorError(f"layout hook '{hook_path}' failed (exit {result.returncode}): {result.stderr}")


def _conforms_subprocess_layout_hook_runner(x: SubprocessLayoutHookRunner) -> ILayoutHookRunner:
    return x
