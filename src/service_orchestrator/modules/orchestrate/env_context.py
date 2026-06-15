"""Per-action resolved environment context.

``EnvContext`` carries the concrete, per-invocation values that both doors
(``cli.py`` and ``env_cli.py``) derive before calling the single
``OrchestratorService``.  Building an ``EnvContext`` — loading the manifest,
resolving the env file path — is the DOOR's responsibility in Phase 3.
This module only defines the dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from service_manifest.modules.manifest.model import ServiceManifest


@dataclass(frozen=True)
class EnvContext:
    """Resolved per-action state for one orchestrator invocation.

    Attributes:
        env: The feature environment name (e.g. ``"alpha"``).
        workspace_root: Absolute path to the workspace root.
        worktree_dir: Absolute path to the per-env worktree
            (``workspace_root / env`` for the default locator).
        manifest: The fully-parsed ``ServiceManifest`` for this workspace.
        env_vars: Parsed key-value mapping from the env file, or ``None``
            when no env file was declared or the file was absent.
        env_file_path: Absolute path to the resolved env file, or ``None``
            when not applicable.
    """

    env: str
    workspace_root: Path
    worktree_dir: Path
    manifest: ServiceManifest
    env_vars: dict[str, str] | None
    env_file_path: Path | None

    @property
    def session(self) -> str:
        """The fully-qualified tmux session name: ``<session_prefix>-<env>``."""
        return f"{self.manifest.session_prefix}-{self.env}"
