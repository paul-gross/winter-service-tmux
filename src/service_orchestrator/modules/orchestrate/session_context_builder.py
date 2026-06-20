"""Shared ``SessionContext`` builder used by both CLI doors.

Both ``cli.py`` (name-addressed) and ``env_cli.py`` (env-root symlink) call
``build_session_context`` to resolve workspace root → load manifest → resolve env
file → compute session.  Factoring this here keeps both doors thin and the
logic testable in isolation.

Resolution decisions (see ``00-plan.md`` resolved decisions #1, #2):
- Manifest is always read from the **workspace root** (single shared config).
- Env file path is resolved relative to the **worktree dir** (per-env file).
- ``env_vars=None`` / ``env_file_path=None`` are the "local" mode signals.
"""

from __future__ import annotations

from pathlib import Path

from service_manifest.modules.manifest.env_reader import EnvFileReader
from service_manifest.modules.manifest.reader import ManifestReader
from service_orchestrator.core.workspace_locator import IWorkspaceLocator
from service_orchestrator.modules.orchestrate.session_context import SessionContext

# Sentinel so callers can distinguish "pass None explicitly" from "omit".
_SENTINEL: object = object()

# Reserved target token for the workspace-singleton tmux session.
# Phase 4 (cli.py/env_cli.py) intercepts this before passing to the env-scoped build().
WORKSPACE_TARGET = "workspace"


class SessionContextBuilder:
    """Resolves an ``SessionContext`` from a workspace locator + manifest reader.

    Both CLI doors construct one instance (via the ``Container``) and call
    ``build()``.  All filesystem I/O is delegated to the injected services so
    tests can substitute fakes without touching real paths.
    """

    def __init__(
        self,
        locator: IWorkspaceLocator,
        manifest_reader: ManifestReader,
        env_reader: EnvFileReader,
    ) -> None:
        self._locator = locator
        self._manifest_reader = manifest_reader
        self._env_reader = env_reader

    def build(
        self,
        env: str,
        *,
        workspace_root: Path | None = None,
        env_file_override: object = _SENTINEL,
        skip_env_file: bool = False,
    ) -> SessionContext:
        """Build an ``SessionContext`` for *env*.

        Args:
            env: The feature-env name (e.g. ``"alpha"``).
            workspace_root: Override the workspace root.  Defaults to
                ``locator.workspace_root()``.
            env_file_override: Supply an explicit env-file path (already
                resolved, as a ``Path | None``).  Pass ``None`` explicitly to
                force no env file.  Omit (pass the sentinel) to let the
                manifest's ``env_file`` field drive resolution.
            skip_env_file: When ``True``, env file resolution is skipped and
                ``env_vars``/``env_file_path`` are both set to ``None``
                (the "local" mode used by ``env_cli.py``).
        """
        ws_root = workspace_root if workspace_root is not None else self._locator.workspace_root()
        worktree_dir = ws_root / env
        manifest = self._manifest_reader.read(ws_root)

        if skip_env_file:
            env_file_path: Path | None = None
            env_vars: dict[str, str] | None = None
        elif env_file_override is not _SENTINEL:
            env_file_path = env_file_override  # type: ignore[assignment]
            env_vars = self._env_reader.resolve(env_file_path)
        else:
            # Derive from manifest declaration: resolve relative to worktree.
            env_file_path = worktree_dir / manifest.env_file if manifest.env_file is not None else None
            env_vars = self._env_reader.resolve(env_file_path)

        return SessionContext(
            env=env,
            workspace_root=ws_root,
            worktree_dir=worktree_dir,
            session_prefix=manifest.session_prefix,
            services=manifest.services,
            layout_hook=manifest.layout_hook,
            status_urls=manifest.status_urls,
            logs=manifest.logs,
            env_vars=env_vars,
            env_file_path=env_file_path,
        )

    def build_workspace(
        self,
        *,
        workspace_root: Path | None = None,
    ) -> SessionContext:
        """Build an ``SessionContext`` for the workspace-singleton session.

        The workspace session (``<prefix>-workspace``) runs at the workspace
        root, not inside a per-env worktree.  The critical fork from ``build()``
        is that ``worktree_dir`` is set to ``ws_root`` (NOT ``ws_root/workspace``)
        so the orchestrator creates the tmux session with cwd=ws_root and writes
        logs under ``<ws_root>/.winter/logs/``.

        The session's services/layout are selected from the manifest's
        ``workspace_*`` fields (status URLs are dropped — the workspace header
        must not render env URLs), so ``OrchestratorService`` consumes them
        identically to a feature-env session.  No env file is loaded
        (``env_vars=None``, ``env_file_path=None``).
        """
        ws_root = workspace_root if workspace_root is not None else self._locator.workspace_root()
        manifest = self._manifest_reader.read(ws_root)
        return SessionContext(
            env=WORKSPACE_TARGET,
            workspace_root=ws_root,
            worktree_dir=ws_root,
            session_prefix=manifest.session_prefix,
            services=manifest.workspace_services,
            layout_hook=manifest.workspace_layout_hook,
            status_urls=(),
            logs=manifest.logs,
            env_vars=None,
            env_file_path=None,
        )


def build_for_target(
    builder: SessionContextBuilder,
    target: str,
    *,
    workspace_root: Path | None = None,
) -> SessionContext:
    """Dispatcher: route *target* to the correct context builder.

    When *target* is ``WORKSPACE_TARGET`` (``"workspace"``), delegates to
    ``builder.build_workspace()``; otherwise delegates to ``builder.build()``.

    This is the single seam that Phase 4 (cli.py / env_cli.py) and any
    enumeration loops use so the literal ``"workspace"`` token never goes
    through the env-scoped ``build()``.

    Args:
        builder: The ``SessionContextBuilder`` instance.
        target: Either ``WORKSPACE_TARGET`` or a feature-env name (e.g. ``"alpha"``).
        workspace_root: Optional workspace root override forwarded to the
            underlying builder method.
    """
    if target == WORKSPACE_TARGET:
        return builder.build_workspace(workspace_root=workspace_root)
    return builder.build(target, workspace_root=workspace_root)
