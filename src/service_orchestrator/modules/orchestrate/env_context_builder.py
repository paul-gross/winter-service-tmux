"""Shared ``EnvContext`` builder used by both CLI doors.

Both ``cli.py`` (name-addressed) and ``env_cli.py`` (env-root symlink) call
``build_env_context`` to resolve workspace root → load manifest → resolve env
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
from service_orchestrator.modules.orchestrate.env_context import EnvContext

# Sentinel so callers can distinguish "pass None explicitly" from "omit".
_SENTINEL: object = object()


class EnvContextBuilder:
    """Resolves an ``EnvContext`` from a workspace locator + manifest reader.

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
    ) -> EnvContext:
        """Build an ``EnvContext`` for *env*.

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

        return EnvContext(
            env=env,
            workspace_root=ws_root,
            worktree_dir=worktree_dir,
            manifest=manifest,
            env_vars=env_vars,
            env_file_path=env_file_path,
        )
