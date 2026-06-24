"""Shared ``SessionContext`` builder used by both CLI doors.

Both ``cli.py`` (name-addressed) and ``env_cli.py`` (env-root symlink) call
``build_session_context`` to resolve workspace root → load manifest → resolve env
file → compute session.  Factoring this here keeps both doors thin and the
logic testable in isolation.

Resolution decisions (see ``00-plan.md`` resolved decisions #1, #2, R1):
- Manifest is read from the **config dir** (``locator.config_dir()``).
- ``layout_hook`` / ``workspace_layout_hook`` are resolved relative to the
  **config dir** — they are bare filenames in the manifest.
- Env file path is resolved relative to the **worktree dir** (per-env file).
- ``env_vars=None`` / ``env_file_path=None`` are the "local" mode signals.

Extension-declared services
----------------------------
When ``WINTER_SERVICE_MANIFEST`` is set in the environment, the builder reads
that TOML file after loading the committed ``config.toml`` and merges any
extension-declared services into the manifest.  Services without a ``target``
field are skipped (tmux requires a pane address).  Providers that predate this
contract ignore the env var.
"""

from __future__ import annotations

import os
from pathlib import Path

from service_manifest.modules.manifest.env_reader import EnvFileReader
from service_manifest.modules.manifest.ext_reader import ExtManifestMerger
from service_manifest.modules.manifest.model import ServiceManifest
from service_manifest.modules.manifest.reader import ManifestReader
from service_orchestrator.core.workspace_locator import IWorkspaceLocator
from service_orchestrator.modules.orchestrate.session_context import SessionContext

# Env-var name set by winter-cli when extension-declared services are present.
_EXT_MANIFEST_ENV = "WINTER_SERVICE_MANIFEST"

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
        cfg_dir = self._locator.config_dir()
        worktree_dir = ws_root / env
        manifest = self._manifest_reader.read(cfg_dir)
        manifest = _apply_ext_manifest(manifest)

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
            config_dir=cfg_dir,
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
        cfg_dir = self._locator.config_dir()
        manifest = self._manifest_reader.read(cfg_dir)
        manifest = _apply_ext_manifest(manifest)
        return SessionContext(
            env=WORKSPACE_TARGET,
            workspace_root=ws_root,
            worktree_dir=ws_root,
            config_dir=cfg_dir,
            session_prefix=manifest.session_prefix,
            services=manifest.workspace_services,
            layout_hook=manifest.workspace_layout_hook,
            status_urls=(),
            logs=manifest.logs,
            env_vars=None,
            env_file_path=None,
        )


def _apply_ext_manifest(manifest: ServiceManifest) -> ServiceManifest:
    """Merge extension-declared services from ``WINTER_SERVICE_MANIFEST`` if set.

    Reads the env var at call time (not at import time) so tests can set it
    without module-level side effects.  Returns the original manifest unmodified
    when the env var is absent or the file cannot be read.
    """
    ext_path_str = os.environ.get(_EXT_MANIFEST_ENV)
    if not ext_path_str:
        return manifest
    ext_path = Path(ext_path_str)
    return ExtManifestMerger().read_and_merge(manifest, ext_path)


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
