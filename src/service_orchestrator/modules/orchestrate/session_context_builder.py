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
- ``inject_scope=None`` is the local/env-less signal for pane scope-sourcing.
- ``env_file_path`` is the resolved absolute path to the manifest machine-creds
  file (``manifest.env_file`` joined to ``worktree_dir``); it drives POSIX
  dot-sourcing in the pane launch prefix and is set independently of
  ``skip_env_file`` (which only governs in-process ``env_vars`` resolution).

Extension-declared services
----------------------------
When ``WINTER_SERVICE_MANIFEST`` is set in the environment, the builder reads
that TOML file after loading the committed ``config.toml`` and merges any
extension-declared services into the manifest.  Services without a ``target``
field are skipped (tmux requires a pane address).  Providers that predate this
contract ignore the env var.

Session-name prefix resolution
-------------------------------
The tmux session-name prefix is resolved by ``_resolve_session_prefix``: the
manifest's optional ``session_prefix`` override wins when declared; otherwise
the builder falls back to the ``WINTER_SERVICE_PREFIX`` environment variable,
a base extension var that winter injects into the provider process on every
dispatched action (see
``workspace:/context/winter-cli/contracts/service-orchestrator.md``).
"""

from __future__ import annotations

import os
from pathlib import Path

from service_manifest.modules.manifest.env_reader import EnvFileReader
from service_manifest.modules.manifest.ext_reader import ExtManifestMerger
from service_manifest.modules.manifest.model import ServiceManifest
from service_manifest.modules.manifest.reader import ManifestReader
from service_orchestrator.core.workspace_locator import IWorkspaceLocator
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.session_context import SessionContext

# Env-var name set by winter-cli when extension-declared services are present.
_EXT_MANIFEST_ENV = "WINTER_SERVICE_MANIFEST"

# Env-var name winter-cli injects on every dispatched action (a base extension
# var) carrying the resolved workspace-level service-orchestration namespace
# prefix.
_SERVICE_PREFIX_ENV = "WINTER_SERVICE_PREFIX"

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
            skip_env_file: When ``True``, in-process env-file resolution is
                skipped and ``env_vars`` is set to ``None`` (the path used by
                ``env_cli.py``).  ``env_file_path`` is still set from the
                manifest — it drives pane dot-sourcing independently.
        """
        ws_root = workspace_root if workspace_root is not None else self._locator.workspace_root()
        cfg_dir = self._locator.config_dir()
        worktree_dir = ws_root / env
        manifest = self._manifest_reader.read(cfg_dir)
        manifest = _apply_ext_manifest(manifest)

        # env_file_path: always derive from the manifest for pane dot-sourcing,
        # regardless of skip_env_file (which only governs in-process env_vars).
        env_file_path = worktree_dir / manifest.env_file if manifest.env_file is not None else None

        if skip_env_file:
            env_vars: dict[str, str] | None = None
        elif env_file_override is not _SENTINEL:
            env_vars = self._env_reader.resolve(env_file_override)  # type: ignore[arg-type]
        else:
            # Derive from manifest declaration: resolve relative to worktree.
            env_vars = self._env_reader.resolve(env_file_path)

        return SessionContext(
            env=env,
            workspace_root=ws_root,
            worktree_dir=worktree_dir,
            config_dir=cfg_dir,
            session_prefix=_resolve_session_prefix(manifest.session_prefix),
            services=manifest.services,
            layout_hook=manifest.layout_hook,
            logs=manifest.logs,
            env_vars=env_vars,
            inject_scope=env,
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
        ``workspace_*`` fields, so ``OrchestratorService`` consumes them
        identically to a feature-env session.  No env file is loaded
        (``env_vars=None``, ``env_file_path=None`` — workspace services do not
        dot-source a per-env machine-creds file).
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
            session_prefix=_resolve_session_prefix(manifest.session_prefix),
            services=manifest.workspace_services,
            layout_hook=manifest.workspace_layout_hook,
            logs=manifest.logs,
            env_vars=None,
            inject_scope=None,
            env_file_path=None,
        )


def _resolve_session_prefix(manifest_override: str | None) -> str:
    """Resolve the tmux session-name prefix.

    ``manifest_override`` — the manifest's optional ``session_prefix`` key —
    always wins when declared (a per-provider escape hatch). ``WINTER_SERVICE_PREFIX``
    is a base extension var present on every dispatched action
    (``up``/``down``/``status``/``restart``/``logs``), so this override is not
    needed to cover any of those doors; it exists for the genuine residual
    case — a raw/direct invocation of this provider's entrypoint outside
    ``winter service`` dispatch entirely, with a stripped/minimal environment
    that never had the var injected. Otherwise falls back to the
    ``WINTER_SERVICE_PREFIX`` environment variable.

    Raises ``OrchestratorError`` when neither source yields a value — this
    happens when the provider is invoked without winter's env injection (e.g.
    directly, outside a winter dispatch) and no manifest override is declared.
    """
    if manifest_override is not None:
        return manifest_override
    env_prefix = os.environ.get(_SERVICE_PREFIX_ENV)
    if env_prefix:
        return env_prefix
    raise OrchestratorError(
        "cannot resolve the tmux session-name prefix: neither the manifest's "
        f"'session_prefix' override nor the {_SERVICE_PREFIX_ENV} environment "
        "variable is set"
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
    skip_env_file: bool = False,
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
        skip_env_file: When ``True``, forwarded to ``builder.build()`` so that
            env-file reading is skipped on the status path.  Ignored for the
            workspace target (``build_workspace`` never reads an env file).
    """
    if target == WORKSPACE_TARGET:
        return builder.build_workspace(workspace_root=workspace_root)
    return builder.build(target, workspace_root=workspace_root, skip_env_file=skip_env_file)
