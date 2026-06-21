"""Name-addressed entrypoint door — conforms to the winter ``orchestrate_services`` contract.

Winter always invokes this as::

    <entrypoint> <action> [<pattern> ...]

``<action>`` is one of ``up``, ``down``, ``status``, ``restart``, ``logs``.

Action-specific argv shapes (patterns are ``<env>/<svc>`` segment-globs):

- ``up <env>``              — single env, no patterns
- ``down <env>``            — single env, no patterns
- ``status [<pattern>...]`` — 0 or more patterns; 0 means all running envs
- ``restart <pattern>...``  — 1 or more patterns (non-zero if none given)
- ``logs <pattern>...``     — 1 or more patterns (non-zero if none given)

Per-action parameters arrive via ``WINTER_*`` environment variables:
- ``WINTER_WORKSPACE_DIR`` — absolute path to the workspace root (always set).
- ``WINTER_EXT_DIR``       — path to the extension clone (always set).
- ``WINTER_EXT_PREFIX``    — resolved symlink prefix (always set).
- ``WINTER_LOG_FOLLOW``    — ``1`` = follow, ``0`` = backlog-only (set for ``logs``).
- ``WINTER_LOG_TAIL``      — positive int or ``all`` (set for ``logs``).
- ``WINTER_LOG_SINCE``     — RFC3339 timestamp lower bound; empty if unset (set for ``logs``).
- ``WINTER_LOG_UNTIL``     — RFC3339 timestamp upper bound; empty if unset (set for ``logs``).
- ``WINTER_LOG_TIMESTAMPS``— ``1`` = timestamps requested (set for ``logs``).

Exit-code passthrough: the orchestrator's return value becomes this process's
exit code, as required by the contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from service_orchestrator.container import Container
from service_orchestrator.modules.orchestrate.env_enumerator import running_envs
from service_orchestrator.modules.orchestrate.request_parser import (
    ParseError,
    parse_request,
)
from service_orchestrator.modules.orchestrate.session_context_builder import (
    WORKSPACE_TARGET,
    build_for_target,
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
    """
    selector = container.selector
    dispatch = container.dispatch

    if not patterns:
        # 0 patterns → status all running envs.
        sessions = container.tmux.list_sessions()
        if not sessions:
            return [], 0

        # Derive prefix from the first session to enumerate envs.
        # Prefer a non-workspace seed for prefix resolution (workspace ctx
        # carries the same prefix, but a regular env seed is more robust when
        # workspace_services is empty).
        candidate_session = sessions[0]
        candidate_env_name = candidate_session.split("-", 1)[1] if "-" in candidate_session else candidate_session
        if candidate_env_name == WORKSPACE_TARGET and len(sessions) > 1:
            alt = sessions[1]
            candidate_env_name = alt.split("-", 1)[1] if "-" in alt else alt
        try:
            seed_ctx = build_for_target(
                container.session_context_builder,
                candidate_env_name,
                workspace_root=workspace_root,
            )
        except Exception as exc:
            print(f"orchestrate: could not read manifest: {exc}", file=sys.stderr)
            return [], 1

        prefix = seed_ctx.session_prefix
        envs = running_envs(container.tmux, prefix)
        return dispatch.collect_status_all_envs(envs, workspace_root)

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
    workspace_root: Path | None,
) -> int:
    """Handle logs action: 1 or more patterns."""
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

    _env = os.environ
    follow = _env.get("WINTER_LOG_FOLLOW") == "1"

    if follow:
        return dispatch.logs_follow(env_services, workspace_root, _env)

    return dispatch.logs_backlog(env_services, workspace_root, _env)


def main(argv: list[str]) -> int:
    """Parse ``[action, *rest]`` and dispatch to ``OrchestratorService`` or ``LogService``.

    Returns an integer exit code (0 = success, non-zero = failure).
    Catches ``OrchestratorError`` and ``ManifestError`` at this boundary;
    everything else propagates.
    """
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
    # logs: 1 or more patterns required
    # ------------------------------------------------------------------
    if action == "logs":
        return _run_logs(container, request.patterns, workspace_root)

    # Should be unreachable — already guarded above.
    print(f"orchestrate: internal error: unhandled action '{action}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
