"""Env-root door — preserves the ``./up`` / ``./down`` / ``./status`` / ``./restart``
ergonomics for direct invocation from a feature env directory.

Each shim script (``workflow/up``, etc.) invokes this module as::

    python3 -m service_orchestrator.env_cli <action> [args...]

**Delegation model.** The user-facing actions delegate to the workspace-level
``winter service <action> <env>`` command so they fan out across *every* bound
service provider (capability dispatch), not just this tmux orchestrator. This is
what lets ``./up`` also start the docker-backed services a workspace registers,
and keeps a single source of truth for lifecycle behavior (workspace-scope
auto-start, startup-retry, …). The one value this door adds on top of ``winter
service`` is the tmux-only ``-a``/``--attach`` convenience on ``up``.

The sole in-process (tmux-only) path that remains is ``down --tmux-only``, used
by ``hooks/destroy-worktree.sh`` during ``winter ws destroy``: it must tear down
*only* this extension's tmux session (docker teardown is that provider's own
hook's job) and must keep working when the manifest is unreadable — hence the
``_down_suffix_fallback`` env-suffix session kill.

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

- ``up [-a|--attach] [<name>]`` — delegates to ``winter service up <env>``,
  then execs ``tmux attach-session`` when ``-a``.
- ``down [--tmux-only] [<name>]`` — delegates to ``winter service down <env>``;
  with ``--tmux-only`` (destroy hook) takes the in-process tmux session kill
  with env-suffix fallback.
- ``status [--all] [<pattern>...]`` — delegates to ``winter service status``.
- ``restart <pattern>...`` — delegates to ``winter service restart``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from service_manifest.modules.manifest.errors import ManifestError
from service_orchestrator.container import Container
from service_orchestrator.core.internal.env_workspace_locator import EnvWorkspaceLocator
from service_orchestrator.core.internal.subprocess_winter_cli import SubprocessWinterCli
from service_orchestrator.core.winter_cli import IWinterCli
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import (
    WORKSPACE_TARGET,
    SessionContextBuilder,
    build_for_target,
)
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository

_ACTIONS = ("up", "down", "status", "restart")


# ---------------------------------------------------------------------------
# tmux-only SessionContext (down --tmux-only path)
# ---------------------------------------------------------------------------


def _build_env_ctx(
    builder: SessionContextBuilder,
    env: str,
    workspace_root: Path,
) -> SessionContext:
    """Build a ``SessionContext`` for the in-process ``down --tmux-only`` path.

    ``down`` only reaps the session's panes and kills the session — it reads no
    ``env_vars`` (no layout hook runs, no ports are resolved on teardown), so
    the built context is returned as-is with no environment injected.
    """
    return build_for_target(builder, env, workspace_root=workspace_root, skip_env_file=True)


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
# Down env-suffix fallback (tmux-only destroy path)
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


def _handle_up(argv: list[str], env: str, cli: IWinterCli) -> int:
    """``up [-a|--attach] [<env>]`` — delegate to ``winter service up``, then attach.

    Startup-retry and workspace-scope auto-start are inherited from ``winter
    service up``; the only door-local behavior is the ``-a`` tmux attach.
    """
    attach = False
    for arg in argv:
        if arg in ("-a", "--attach"):
            attach = True
        elif not arg.startswith("-"):
            env = arg  # explicit env name override

    rc = cli.service(["up", env])

    if attach and rc == 0:
        prefix = os.environ.get("WINTER_SERVICE_PREFIX")
        if not prefix:
            print(
                "up: cannot attach: WINTER_SERVICE_PREFIX not set "
                "(the shim sources `winter env` — is winter installed?)",
                file=sys.stderr,
            )
            return rc
        # Session name is `<prefix>-<env>` (see SessionContext.session). The
        # env-var prefix is authoritative for the normal path; a manifest
        # `session_prefix` override would not be reflected here.
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"{prefix}-{env}"])

    return rc


def _handle_down(argv: list[str], env: str, workspace_root: Path, cli: IWinterCli) -> int:
    """``down [--tmux-only] [<env>]``.

    Normal ``./down`` delegates to ``winter service down`` (fans out across
    providers, leaves the workspace scope running). ``--tmux-only`` (passed by
    ``hooks/destroy-worktree.sh``) takes the in-process tmux session kill with
    an env-suffix fallback, so ``winter ws destroy`` never wedges on an
    unreadable manifest.
    """
    tmux_only = False
    for arg in argv:
        if arg == "--tmux-only":
            tmux_only = True
        elif not arg.startswith("-"):
            env = arg  # explicit env name override

    if not tmux_only:
        return cli.service(["down", env])

    container = Container()
    try:
        if env == WORKSPACE_TARGET:
            ctx = container.session_context_builder.build_workspace(
                workspace_root=workspace_root,
            )
        else:
            ctx = _build_env_ctx(container.session_context_builder, env, workspace_root)
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


def _handle_status(argv: list[str], env: str, cli: IWinterCli) -> int:
    """``status [--all] [<pattern>...]`` — delegate to ``winter service status``.

    ``--all`` reports every env across every provider (``winter service status``
    with no target). Bare patterns are prefixed with this env's segment so
    winter's own glob routing selects within the env.
    """
    show_all = False
    patterns: list[str] = []
    for arg in argv:
        if arg in ("--all", "all"):
            show_all = True
        else:
            patterns.append(arg)

    if show_all:
        if patterns:
            print("status: --all takes no service patterns", file=sys.stderr)
            return 2
        return cli.service(["status"])

    if not patterns:
        return cli.service(["status", env])

    return cli.service(["status", *(f"{env}/{pat}" for pat in patterns)])


def _handle_restart(argv: list[str], env: str, cli: IWinterCli) -> int:
    """``restart <pattern>...`` — delegate to ``winter service restart``.

    Each bare pattern is prefixed with this env's segment; winter routes each
    matched service to its owning provider, so ``./restart db`` can bounce a
    docker-backed service just as ``./restart api`` bounces a tmux one.
    """
    if not argv or argv[0].startswith("-"):
        if not argv:
            print("usage: restart <pattern>...", file=sys.stderr)
        else:
            print(f"restart: takes a service pattern, not a flag: '{argv[0]}'", file=sys.stderr)
        return 1

    return cli.service(["restart", *(f"{env}/{pat}" for pat in argv)])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str], *, winter_cli: IWinterCli | None = None) -> int:
    """Parse ``[action, ...]`` and dispatch.

    ``argv`` should be ``sys.argv[1:]`` (action word + remaining args).
    ``sys.argv[0]`` is used to resolve the env name and workspace root from
    the symlink location. ``winter_cli`` is the ``winter service`` delegation
    seam; the real subprocess adapter is used unless a fake is injected (tests).
    """
    cli = winter_cli or SubprocessWinterCli()

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

    if action == "up":
        return _handle_up(rest, env, cli)
    elif action == "down":
        return _handle_down(rest, env, workspace_root, cli)
    elif action == "status":
        return _handle_status(rest, env, cli)
    elif action == "restart":
        return _handle_restart(rest, env, cli)
    else:
        print(f"internal error: unhandled action '{action}'", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
