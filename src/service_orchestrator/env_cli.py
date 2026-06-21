"""Env-root symlink door — preserves the ``./up`` / ``./down`` / ``./status`` / ``./restart``
ergonomics for direct invocation from a feature env directory.

Each shim script (``workflow/up``, etc.) invokes this module as::

    python3 -m service_orchestrator.env_cli <action> [args...]

Workspace-root / env-name resolution order:

1. ``WINTER_WORKSPACE_DIR`` environment variable (authoritative when set).
   Used by ``hooks/destroy-worktree.sh``, which invokes the extension's
   ``workflow/down`` directly (not via an env-root symlink) and sets both
   ``WINTER_WORKSPACE_DIR`` and ``WINTER_ENV``.  The env name in that path
   comes from the explicit positional arg, not argv0.
2. argv0 symlink inference: when invoked as ``<workspace>/<env>/<script>``,
   the symlink's parent directory gives both env name and workspace root.
3. ``IWorkspaceLocator`` marker-walk for ``.winter/config.toml`` (last resort
   when argv0 cannot be resolved as a per-env symlink).

Door-local arg shapes (NOT part of the winter contract):

- ``up [local] [-a|--attach] [<name>]``
- ``down [local] [<name>]``
- ``status [--all] [<pattern>...]``
- ``restart <pattern>...``

``local`` mode builds an env-less ``SessionContext`` (no env file sourced);
``up local`` and ``down local`` are symmetric — both use the inferred env
name with ``skip_env_file=True``.
``-a``/``--attach`` execs ``tmux attach-session`` after ``up``.
``status --all`` loops every ``<prefix>-`` session.
``down`` falls back to env-suffix session matching when the manifest is
unreadable (so ``winter ws destroy`` never gets stuck).
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

from service_manifest.modules.manifest.errors import ManifestError
from service_orchestrator.container import Container
from service_orchestrator.core.internal.env_workspace_locator import EnvWorkspaceLocator
from service_orchestrator.modules.orchestrate.env_enumerator import running_envs
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.session_context_builder import (
    WORKSPACE_TARGET,
    build_for_target,
)
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository

_ACTIONS = ("up", "down", "status", "restart")


# ---------------------------------------------------------------------------
# Workspace / env resolution from the symlink path
# ---------------------------------------------------------------------------


def _resolve_from_argv0(argv0: str) -> tuple[Path, str] | None:
    """Follow the symlink chain of *argv0* to find the real env dir.

    Returns ``(workspace_root, env_name)`` or ``None`` when resolution fails
    (e.g. the script was invoked in an unexpected way).
    """
    try:
        # When invoked from a symlink in <workspace>/<env>/<script>, the
        # symlink itself lives in <workspace>/<env>/.  We want the symlink's
        # *parent* directory.  Path(argv0) without resolve() gives us the
        # symlink location.
        link_dir = Path(argv0).parent.resolve()
        env_name = link_dir.name
        workspace_root = link_dir.parent
        if workspace_root.exists() and env_name:
            return workspace_root, env_name
    except Exception:
        pass
    return None


def _locate_workspace_and_env(argv0: str) -> tuple[Path, str]:
    """Resolve workspace root and env name.

    Workspace-root resolution order:
    1. ``WINTER_WORKSPACE_DIR`` env var (authoritative; used by hooks that
       invoke the extension's ``workflow/down`` directly, not via an env-root
       symlink).
    2. argv0 symlink inference — ``<workspace>/<env>/<script>`` → root.
    3. Marker-walk from cwd (last resort).

    Env-name resolution order:
    1. argv0 symlink inference (when argv0 resolves as a per-env symlink).
    2. Empty string — the action handler must pick up the env name from its
       own positional arg, or error with a useful message.
    """
    ws_dir = os.environ.get("WINTER_WORKSPACE_DIR")

    # Try argv0 inference regardless (gives env name when invoked via symlink).
    argv0_result = _resolve_from_argv0(argv0)

    if ws_dir:
        # WINTER_WORKSPACE_DIR wins for workspace root.
        ws_root = Path(ws_dir)
        # Use argv0-inferred env when it came from a per-env symlink AND its
        # workspace matches the one declared in WINTER_WORKSPACE_DIR; otherwise
        # leave env empty for the action handler to supply via positional arg.
        if argv0_result is not None and argv0_result[0] == ws_root:
            return ws_root, argv0_result[1]
        return ws_root, ""

    if argv0_result is not None:
        return argv0_result

    # Fallback: marker-walk from cwd.
    locator = EnvWorkspaceLocator()
    ws_root = locator.workspace_root()
    # Can't determine env name from cwd alone; fall back to empty string and
    # let the caller error out with a useful message.
    return ws_root, ""


# ---------------------------------------------------------------------------
# Down env-suffix fallback (resolved decision #2)
# ---------------------------------------------------------------------------


def _down_suffix_fallback(env: str, tmux_repo: ITmuxRepository) -> int:
    """Match and kill a tmux session by env-name suffix when manifest is unreadable.

    Uses ``tmux_repo.list_sessions()`` filtered to names ending with ``-<env>``.
    """
    try:
        sessions: list[str] = tmux_repo.list_sessions()
    except Exception:
        sessions = []

    matches = [s for s in sessions if s.endswith(f"-{env}")]
    if not matches:
        print(f"No running session for '{env}' (manifest unreadable and no env-suffix match)")
        return 0

    for session in matches:
        print(
            f"warning: manifest unreadable; killing session '{session}' by env-suffix match",
            file=sys.stderr,
        )
        try:
            tmux_repo.kill_session(session)
        except (OSError, OrchestratorError) as exc:
            print(f"warning: kill_session '{session}' failed: {exc}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# action handlers
# ---------------------------------------------------------------------------


def _handle_up(
    argv: list[str],
    env: str,
    workspace_root: Path,
    container: Container,
) -> int:
    local = False
    attach = False
    for arg in argv:
        if arg in ("-a", "--attach"):
            attach = True
        elif arg == "local":
            local = True
        elif not arg.startswith("-"):
            env = arg  # explicit env name override

    try:
        if env == WORKSPACE_TARGET:
            ctx = container.session_context_builder.build_workspace(
                workspace_root=workspace_root,
            )
        else:
            ctx = container.session_context_builder.build(
                env,
                workspace_root=workspace_root,
                skip_env_file=local,
            )
    except ManifestError as exc:
        print(f"up: env '{env}': manifest error: {exc}", file=sys.stderr)
        return 1
    except (OSError, OrchestratorError) as exc:
        print(f"up: env '{env}': {exc}", file=sys.stderr)
        return 1

    try:
        rc = container.orchestrator.up(ctx)
    except OrchestratorError as exc:
        print(f"up: env '{env}': {exc}", file=sys.stderr)
        return 1

    if attach and rc == 0:
        os.execvp("tmux", ["tmux", "attach-session", "-t", ctx.session])

    return rc


def _handle_down(
    argv: list[str],
    env: str,
    workspace_root: Path,
    container: Container,
) -> int:
    local = False
    for arg in argv:
        if arg == "local":
            local = True
        elif not arg.startswith("-"):
            env = arg

    try:
        if env == WORKSPACE_TARGET:
            ctx = container.session_context_builder.build_workspace(
                workspace_root=workspace_root,
            )
        else:
            ctx = container.session_context_builder.build(
                env,
                workspace_root=workspace_root,
                skip_env_file=local,
            )
    except ManifestError as exc:
        # Manifest unreadable → fall back to env-suffix session kill.
        print(
            f"down: manifest unreadable for env '{env}': {exc}; falling back to env-suffix session match",
            file=sys.stderr,
        )
        return _down_suffix_fallback(env, container.tmux)
    except (OSError, OrchestratorError) as exc:
        print(f"down: env '{env}': {exc}", file=sys.stderr)
        return _down_suffix_fallback(env, container.tmux)

    try:
        return container.orchestrator.down(ctx)
    except OrchestratorError as exc:
        print(f"down: env '{env}': {exc}", file=sys.stderr)
        return 1


def _handle_status(
    argv: list[str],
    env: str,
    workspace_root: Path,
    container: Container,
) -> int:
    show_all = False
    patterns: list[str] = []
    for arg in argv:
        if arg in ("--all", "all"):
            show_all = True
        else:
            patterns.append(arg)

    if show_all:
        if patterns:
            print(
                "status: --all takes no service patterns",
                file=sys.stderr,
            )
            return 2
        return _handle_status_all(env, workspace_root, container)

    try:
        ctx = build_for_target(container.session_context_builder, env, workspace_root=workspace_root)
    except ManifestError as exc:
        print(f"status: env '{env}': manifest error: {exc}", file=sys.stderr)
        return 1
    except (OSError, OrchestratorError) as exc:
        print(f"status: env '{env}': {exc}", file=sys.stderr)
        return 1

    if not patterns:
        # No patterns — show all services in this env.
        try:
            return container.orchestrator.status(ctx)
        except OrchestratorError as exc:
            print(f"status: env '{env}': {exc}", file=sys.stderr)
            return 1

    # Expand each pattern against this env's services.
    all_names = [svc.name for svc in ctx.services]
    matched: list[str] = []
    for pat in patterns:
        hits = [name for name in all_names if fnmatch.fnmatchcase(name, pat)]
        if not hits:
            print(
                f"status: pattern '{pat}' matches no declared services; declared: {', '.join(all_names) or '(none)'}",
                file=sys.stderr,
            )
            return 1
        for name in hits:
            if name not in matched:
                matched.append(name)

    try:
        return container.orchestrator.status(ctx, services=tuple(matched))
    except OrchestratorError as exc:
        print(f"status: env '{env}': {exc}", file=sys.stderr)
        return 1


def _handle_status_all(
    env: str,
    workspace_root: Path,
    container: Container,
) -> int:
    """Report status for every running session under the configured prefix.

    Reads the manifest to get the session prefix, then lists all tmux sessions
    matching ``<prefix>-`` and reports each.
    """
    try:
        seed_ctx = build_for_target(container.session_context_builder, env, workspace_root=workspace_root)
        prefix = seed_ctx.session_prefix
    except (ManifestError, OSError) as exc:
        print(f"status --all: cannot read manifest: {exc}", file=sys.stderr)
        return 1

    try:
        env_names = running_envs(container.tmux, prefix)
    except OrchestratorError as exc:
        print(f"status --all: {exc}", file=sys.stderr)
        return 1
    if not env_names:
        print(f"No {prefix}-* sessions running.")
        return 0

    rc = 0
    for env_name in env_names:
        try:
            # Risk #1: running_envs() returns the literal "workspace" for the
            # <prefix>-workspace session.  Route through build_for_target so it
            # never goes through env-scoped build() (which would set
            # worktree_dir = ws_root/workspace instead of ws_root itself).
            env_ctx = build_for_target(container.session_context_builder, env_name, workspace_root=workspace_root)
            r = container.orchestrator.status(env_ctx)
            if r != 0:
                rc = r
        except (ManifestError, OrchestratorError, OSError) as exc:
            print(f"  {env_name}: error: {exc}", file=sys.stderr)
            rc = 1
    return rc


def _handle_restart(
    argv: list[str],
    env: str,
    workspace_root: Path,
    container: Container,
) -> int:
    if not argv or argv[0].startswith("-"):
        try:
            ctx = build_for_target(container.session_context_builder, env, workspace_root=workspace_root)
            declared = ", ".join(s.name for s in ctx.services)
        except (ManifestError, OSError, OrchestratorError):
            declared = "(manifest unreadable)"
        if not argv:
            print("usage: restart <pattern>...", file=sys.stderr)
        else:
            print(f"restart: takes a service pattern, not a flag: '{argv[0]}'", file=sys.stderr)
        print(f"declared services: {declared}", file=sys.stderr)
        return 1

    try:
        ctx = build_for_target(container.session_context_builder, env, workspace_root=workspace_root)
    except ManifestError as exc:
        print(f"restart: env '{env}': manifest error: {exc}", file=sys.stderr)
        return 1
    except (OSError, OrchestratorError) as exc:
        print(f"restart: env '{env}': {exc}", file=sys.stderr)
        return 1

    all_names = [svc.name for svc in ctx.services]
    matched: list[str] = []
    for pat in argv:
        hits = [name for name in all_names if fnmatch.fnmatchcase(name, pat)]
        if not hits:
            print(
                f"restart: pattern '{pat}' matches no declared services; declared: {', '.join(all_names) or '(none)'}",
                file=sys.stderr,
            )
            return 1
        for name in hits:
            if name not in matched:
                matched.append(name)

    rc = 0
    for name in matched:
        try:
            r = container.orchestrator.restart(ctx, name)
            if r != 0:
                rc = r
        except OrchestratorError as exc:
            print(f"restart: env '{env}': service '{name}': {exc}", file=sys.stderr)
            rc = 1
    return rc


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    """Parse ``[action, ...]`` and dispatch.

    ``argv`` should be ``sys.argv[1:]`` (action word + remaining args).
    ``sys.argv[0]`` is used to resolve the env name and workspace root from
    the symlink location.
    """
    if not argv:
        print(
            f"usage: <action> [args...]\n  action: {', '.join(_ACTIONS)}",
            file=sys.stderr,
        )
        return 2

    action = argv[0]
    rest = argv[1:]

    if action not in _ACTIONS:
        print(
            f"unknown action '{action}' (expected one of: {', '.join(_ACTIONS)})",
            file=sys.stderr,
        )
        return 2

    workspace_root, env = _locate_workspace_and_env(sys.argv[0])

    # Guard: refuse to run directly in the project/ source checkout.
    if env == "project":
        print(
            f"error: cannot run services directly in project/\n  Run from a feature worktree (e.g. alpha/{action})",
            file=sys.stderr,
        )
        return 1

    # When WINTER_WORKSPACE_DIR provided the workspace root, env is "" here
    # because the env name comes from the action's positional arg (e.g. the
    # destroy hook passes it as "$WINTER_ENV").  Skip the guard and let the
    # action handler pick up the env name from its positional arg.
    if not env and not os.environ.get("WINTER_WORKSPACE_DIR"):
        print(
            f"error: could not determine env name from invocation path '{sys.argv[0]}'",
            file=sys.stderr,
        )
        return 1

    container = Container()

    if action == "up":
        return _handle_up(rest, env, workspace_root, container)
    elif action == "down":
        return _handle_down(rest, env, workspace_root, container)
    elif action == "status":
        return _handle_status(rest, env, workspace_root, container)
    elif action == "restart":
        return _handle_restart(rest, env, workspace_root, container)
    else:
        print(f"internal error: unhandled action '{action}'", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
