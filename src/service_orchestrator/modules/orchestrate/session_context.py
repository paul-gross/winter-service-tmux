"""Per-action resolved session context.

``SessionContext`` carries the concrete, per-invocation values that both doors
(``cli.py`` and ``env_cli.py``) derive before calling the single
``OrchestratorService``.  It is **scope-agnostic**: the same shape describes a
feature-env session (``env == "alpha"``) and the shared workspace-singleton
session (``env == "workspace"``).  Each build path selects the right scope's
service/layout/status values from the manifest and stores them here directly —
there is no env-shaped-manifest projection.  Building a ``SessionContext`` —
loading the manifest, selecting the scope — is the builder's responsibility;
this module only defines the dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from service_manifest.modules.manifest.model import LogConfig, Service


@dataclass(frozen=True)
class SessionContext:
    """Resolved per-action state for one orchestrator invocation.

    The service/layout fields are already scope-selected by the builder:
    for a feature env they are the manifest's env-scoped values; for the
    workspace session they are its ``workspace_*`` values.
    The orchestrator consumes them uniformly without knowing which scope it is.

    Attributes:
        env: The session name segment — a feature-env name (e.g. ``"alpha"``)
            or the reserved ``"workspace"`` token.
        workspace_root: Absolute path to the workspace root.
        worktree_dir: Absolute path to the directory the session runs in — the
            per-env worktree (``workspace_root / env``) for a feature env, or
            ``workspace_root`` itself for the workspace session.
        config_dir: Absolute path to the extension config directory containing
            ``config.toml`` and (optionally) ``config.local.toml``.  Also used
            to resolve ``layout_hook`` and ``workspace_layout_hook`` — those
            values are bare filenames relative to this directory.
        session_prefix: Tmux session-name prefix; the session is
            ``<session_prefix>-<env>``.
        services: The services to run in this session, in declaration order.
        layout_hook: Optional bash layout hook for this session (bare filename,
            resolved relative to ``config_dir``), or ``None``.
        logs: Log-capture configuration.
        env_vars: Key-value mapping available in-process (used by the layout
            hook and port/health resolution), or ``None`` when not applicable.
            The dispatched service door overlays this with ``os.environ``.
            Runtime service-environment resolution obtains the canonical scope
            and shell-sourced env_file snapshot through the orchestrator's
            environment-source seam, so this field is not a parsed env-file
            substitute.
        inject_scope: When not ``None``, each pane's launch prefix includes
            ``eval "$(winter env <inject_scope>)"`` so the pane shell
            self-sources the full scope environment.  ``None`` for local/env-
            less mode (e.g. ``./up local``). The workspace context uses the
            explicit ``"workspace"`` scope baseline.
        env_file_path: Absolute path to the manifest's machine-credentials env
            file (e.g. ``<worktree>/.env.local``), or ``None`` when the
            manifest declares no ``env_file``.  When not ``None``, each pane's
            launch prefix appends ``&& . '<env_file_path>'`` after the
            ``eval "$(winter env ...)"`` segment so machine-specific vars
            (credentials not managed by core) are also available to service
            commands.  This is independent of ``inject_scope``: the file is
            sourced even when ``inject_scope`` is not ``None``, and vice versa.
            ``None`` in local/env-less mode. Workspace services use the
            canonical ``winter env workspace`` source and do not dot-source a
            per-env machine-creds file.
    """

    env: str
    workspace_root: Path
    worktree_dir: Path
    config_dir: Path
    session_prefix: str
    services: tuple[Service, ...]
    layout_hook: str | None
    logs: LogConfig
    env_vars: dict[str, str] | None
    inject_scope: str | None
    env_file_path: Path | None

    @property
    def session(self) -> str:
        """The fully-qualified tmux session name: ``<session_prefix>-<env>``."""
        return f"{self.session_prefix}-{self.env}"
