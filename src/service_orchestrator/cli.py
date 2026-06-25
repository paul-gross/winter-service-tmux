"""Name-addressed entrypoint door — conforms to the winter ``orchestrate_services`` contract.

Winter always invokes this as::

    <entrypoint> <action> [<pattern> ...]

``<action>`` is one of ``up``, ``down``, ``status``, ``restart``, ``logs``.

Action-specific argv shapes (patterns are ``<env>/<svc>`` segment-globs):

- ``up <env>``              — single env, no patterns
- ``down <env>``            — single env, no patterns
- ``status [<pattern>...]`` — 0 or more patterns; core always supplies a scope-qualified pattern
- ``restart <pattern>...``  — 1 or more patterns (non-zero if none given)
- ``logs <pattern>... [render flags]`` — 1 or more patterns, plus render flags

The ``logs`` action's render options arrive as CLI flags appended after the
positional patterns (parsed by ``parse_log_args``), mirroring ``winter service
logs``' own surface::

    logs <pattern>... [-f|--follow] [-n|--tail <N|all>] \\
      [--since <rfc3339>] [--until <rfc3339>] [-t|--timestamps]

``--since``/``--until`` carry winter's already-resolved RFC3339 values (consumed
as-is, never re-parsed as durations); ``--tail`` carries the resolved count
string (``N`` or ``all``).

Base extension parameters arrive via ``WINTER_*`` environment variables:
- ``WINTER_WORKSPACE_DIR`` — absolute path to the workspace root (always set).
- ``WINTER_EXT_DIR``       — path to the extension clone (always set).
- ``WINTER_EXT_PREFIX``    — resolved symlink prefix (always set).

Exit-code passthrough: the orchestrator's return value becomes this process's
exit code, as required by the contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from service_orchestrator.container import Container
from service_orchestrator.modules.orchestrate.log_query import (
    LogRenderOptions,
    parse_log_args,
)
from service_orchestrator.modules.orchestrate.request_parser import (
    ParseError,
    parse_request,
)
from service_orchestrator.modules.orchestrate.status_report import build_status_document


def _collect_status_docs(
    container: Container,
    patterns: list[str],
    workspace_root: Path | None,
) -> tuple[list[dict], int]:  # type: ignore[type-arg]
    """Collect per-env status document fragments for the requested *patterns*.

    Returns ``(env_docs, rc)``.  Diagnostics go to stderr; the caller owns
    serialising the aggregate document to stdout exactly once.

    On the status path, core (winter-cli) always supplies exactly one
    scope-qualified pattern per invocation (e.g. ``alpha/*`` or
    ``workspace/*``).  The provider must NOT enumerate live tmux sessions
    to discover scopes — core owns scope enumeration.  With 0 patterns
    (legacy direct invocation) no scopes are known and an empty document
    is returned.
    """
    selector = container.selector
    dispatch = container.dispatch

    if not patterns:
        # 0 patterns: no scope supplied by core.  Core always passes at least
        # one scope-qualified pattern.  Return an empty document without
        # enumerating live tmux sessions (self-enumeration is removed on the
        # status path per Phase 4 of winter#109).
        return [], 0

    # N patterns → split workspace vs env patterns.
    workspace_pats, env_pats = selector.split_workspace_patterns(patterns)

    docs: list[dict] = []  # type: ignore[type-arg]
    rc = 0

    if workspace_pats:
        ws_docs, rc = dispatch.collect_status_workspace(
            selector,
            workspace_pats,
            workspace_root,
            "status",
            current_rc=rc,
        )
        docs.extend(ws_docs)

    if env_pats:
        manifest_info = selector.read_manifest_context(env_pats, workspace_root)
        if manifest_info is None:
            for pat in env_pats:
                print(
                    f"orchestrate: status: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return docs, 1

        prefix, services_list = manifest_info
        env_services, dead_patterns = selector.expand_env_patterns(env_pats, services_list, prefix, workspace_root)

        if dead_patterns:
            for pat in dead_patterns:
                print(
                    f"orchestrate: status: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return docs, 1

        env_docs, rc = dispatch.collect_status_env_services(env_services, workspace_root, current_rc=rc)
        docs.extend(env_docs)

    return docs, rc


def _run_status(
    container: Container,
    patterns: list[str],
    workspace_root: Path | None,
) -> int:
    """Handle status action: 0 or more patterns.

    Per the winter ``status`` wire contract, the orchestrator **always** emits
    a single schema-valid env-keyed JSON document on stdout — unconditionally,
    regardless of any flag — aggregating every env in scope.  Winter owns
    rendering (human table by default, raw JSON under ``--json``).  Diagnostics
    are written to stderr; only the JSON document reaches stdout.
    """
    docs, rc = _collect_status_docs(container, patterns, workspace_root)
    sys.stdout.write(json.dumps(build_status_document(docs), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return rc


def _run_restart(
    container: Container,
    patterns: list[str],
    workspace_root: Path | None,
) -> int:
    """Handle restart action: 1 or more patterns."""
    selector = container.selector
    dispatch = container.dispatch

    # Intercept workspace patterns before the general pattern engine.
    workspace_pats, env_pats = selector.split_workspace_patterns(patterns)

    rc = 0

    if workspace_pats:
        rc = dispatch.restart_workspace(selector, workspace_pats, workspace_root, rc)
        if rc != 0 and not env_pats:
            return rc

    if env_pats:
        manifest_info = selector.read_manifest_context(env_pats, workspace_root)
        if manifest_info is None:
            for pat in env_pats:
                print(
                    f"orchestrate: restart: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        prefix, services_list = manifest_info
        env_services, dead_patterns = selector.expand_env_patterns(env_pats, services_list, prefix, workspace_root)

        if dead_patterns:
            for pat in dead_patterns:
                print(
                    f"orchestrate: restart: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        rc = dispatch.restart_env_services(env_services, workspace_root, current_rc=rc)

    return rc


def _run_logs(
    container: Container,
    patterns: list[str],
    render: LogRenderOptions,
    workspace_root: Path | None,
) -> int:
    """Handle logs action: 1 or more patterns plus argv-parsed render options."""
    selector = container.selector
    dispatch = container.dispatch

    manifest_info = selector.read_manifest_context(patterns, workspace_root)
    if manifest_info is None:
        for pat in patterns:
            print(
                f"orchestrate: logs: pattern '{pat}' matched no services",
                file=sys.stderr,
            )
        return 1

    prefix, services_list = manifest_info
    env_services, dead_patterns = selector.expand_env_patterns(patterns, services_list, prefix, workspace_root)

    if dead_patterns:
        for pat in dead_patterns:
            print(
                f"orchestrate: logs: pattern '{pat}' matched no services",
                file=sys.stderr,
            )
        return 1

    if render.follow:
        return dispatch.logs_follow(env_services, workspace_root, render)

    return dispatch.logs_backlog(env_services, workspace_root, render)


def _scope_qualified_names(workspace_root: Path | None) -> list[str]:
    """Return scope-qualified service names from the configured manifest.

    Returns ``workspace/<name>`` for workspace-scoped services and ``*/<name>``
    for env-scoped services.  Returns an empty list when no manifest is found.

    # NOTE: describe and catalog emit byte-identical output via this helper today,
    # but they serve distinct contracts (describe → winter ownership index;
    # catalog → lint catalog).  Do not merge the two callers.
    """
    from service_manifest.container import Container as ManifestContainer
    from service_manifest.modules.manifest.errors import ManifestError

    ext_config_dir_val = os.environ.get("WINTER_EXT_CONFIG_DIR")
    if ext_config_dir_val:
        config_dir = Path(ext_config_dir_val)
    elif workspace_root is not None:
        config_dir = workspace_root / ".winter" / "config" / "winter-service-tmux"
    else:
        return []

    manifest_container = ManifestContainer()
    try:
        manifest = manifest_container.manifest_reader.read(config_dir)
    except ManifestError:
        return []

    names: list[str] = []
    for svc in manifest.workspace_services:
        names.append(f"workspace/{svc.name}")
    for svc in manifest.services:
        names.append(f"*/{svc.name}")
    return names


def _run_describe(workspace_root: Path | None) -> int:
    """Handle describe action: emit scope-qualified service names as JSON.

    Outputs ``{"services": ["workspace/<name>", "*/<name>", ...]}`` matching the
    shape required by winter core's service→provider ownership index.
    """
    names = _scope_qualified_names(workspace_root)
    sys.stdout.write(json.dumps({"services": names}) + "\n")
    sys.stdout.flush()
    return 0


def _run_catalog(workspace_root: Path | None) -> int:
    """Handle catalog action: emit scope-qualified service names as JSON.

    Outputs ``{"services": ["workspace/<name>", "*/<name>", ...]}`` where
    ``workspace/<name>`` is a workspace-scoped service and ``*/<name>`` is an
    env-scoped service (env-agnostic, any env may run it).
    """
    names = _scope_qualified_names(workspace_root)
    sys.stdout.write(json.dumps({"services": names}) + "\n")
    sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    """Parse ``[action, *rest]`` and dispatch to ``OrchestratorService`` or ``LogService``.

    Returns an integer exit code (0 = success, non-zero = failure).
    Catches ``OrchestratorError`` and ``ManifestError`` at this boundary;
    everything else propagates.
    """
    # Handle describe/catalog before parse_request (not in the standard action set).
    if argv and argv[0] in ("describe", "catalog"):
        ws_dir = os.environ.get("WINTER_WORKSPACE_DIR")
        workspace_root = Path(ws_dir) if ws_dir else None
        if argv[0] == "describe":
            return _run_describe(workspace_root)
        return _run_catalog(workspace_root)

    request = parse_request(argv)
    if isinstance(request, ParseError):
        print(request.message, file=sys.stderr)
        return request.exit_code

    action = request.action

    container = Container()

    ws_dir = os.environ.get("WINTER_WORKSPACE_DIR")
    workspace_root = Path(ws_dir) if ws_dir else None

    # ------------------------------------------------------------------
    # up / down: single env positional, behavior unchanged
    # ------------------------------------------------------------------
    if action in ("up", "down"):
        env = request.env
        assert env is not None
        if action == "up":
            return container.dispatch.up(env, workspace_root)
        else:
            return container.dispatch.down(env, workspace_root)

    # ------------------------------------------------------------------
    # status: 0 or more patterns
    # ------------------------------------------------------------------
    if action == "status":
        return _run_status(container, request.patterns, workspace_root)

    # ------------------------------------------------------------------
    # restart: 1 or more patterns required
    # ------------------------------------------------------------------
    if action == "restart":
        return _run_restart(container, request.patterns, workspace_root)

    # ------------------------------------------------------------------
    # logs: 1 or more patterns required, plus argv render flags
    # ------------------------------------------------------------------
    if action == "logs":
        patterns, render = parse_log_args(request.patterns)
        if not patterns:
            print("orchestrate: logs requires at least one pattern", file=sys.stderr)
            return 1
        return _run_logs(container, patterns, render, workspace_root)

    # Should be unreachable — already guarded above.
    print(f"orchestrate: internal error: unhandled action '{action}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
