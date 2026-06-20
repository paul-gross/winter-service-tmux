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

import fnmatch
import os
import sys
from pathlib import Path

from service_manifest.modules.manifest.errors import ManifestError
from service_orchestrator.container import Container
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.env_enumerator import running_envs
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.log_query import LogQuery
from service_orchestrator.modules.orchestrate.pattern_match import matches_any_pattern

_ACTIONS = ("up", "down", "status", "restart", "logs")


def _parse_tail(raw: str) -> int | None:
    """Parse ``WINTER_LOG_TAIL`` into an int or None.

    ``"all"`` or empty string → ``None`` (return all events).
    A positive integer string → ``int``.
    Anything else → ``None`` with a stderr warning.
    """
    raw = raw.strip()
    if not raw or raw == "all":
        return None
    try:
        return int(raw)
    except ValueError:
        print(
            f"orchestrate: WINTER_LOG_TAIL '{raw}' is not a valid integer or 'all'; treating as 'all'",
            file=sys.stderr,
        )
        return None


def _is_glob(segment: str) -> bool:
    """Return True when *segment* contains fnmatch wildcard characters."""
    return "*" in segment or "?" in segment or "[" in segment


def _candidate_envs_for_pattern(pattern: str, all_running: list[str]) -> list[str]:
    """Return the list of candidate env names to check for *pattern*.

    A bare pattern (no ``/``) is treated as ``<pattern>/*`` — the env segment
    is the whole token.  If that env segment is a concrete name, return it as
    a singleton; if it is a glob, filter *all_running* against it.
    """
    env_seg = pattern if "/" not in pattern else pattern.split("/", 1)[0]

    if _is_glob(env_seg):
        return [e for e in all_running if fnmatch.fnmatchcase(e, env_seg)]
    return [env_seg]


def _expand_patterns(
    container: Container,
    patterns: list[str],
    workspace_root: Path | None,
    services_list: list[str],
    prefix: str,
) -> tuple[dict[str, list[str]], list[str]]:
    """Expand ``patterns`` against manifest services and running envs.

    The manifest's service names are pre-loaded by the caller (``services_list``)
    and the session prefix (``prefix``) is also provided, avoiding a second
    manifest read.

    Returns:
        ``env_services``: ordered mapping env → list of matched service names
            (in manifest order, preserving only the matched subset).
        ``dead_patterns``: patterns that matched zero (env, svc) pairs.
    """
    tmux = container.tmux

    # Lazily enumerate running envs once when we encounter a glob env-segment.
    live_envs: list[str] | None = None

    env_services: dict[str, list[str]] = {}
    matched_patterns: set[str] = set()

    for pat in patterns:
        env_seg = pat if "/" not in pat else pat.split("/", 1)[0]
        if _is_glob(env_seg) and live_envs is None:
            live_envs = running_envs(tmux, prefix)
        candidate_envs = _candidate_envs_for_pattern(pat, live_envs if live_envs is not None else [])

        for env in candidate_envs:
            for svc_name in services_list:
                if matches_any_pattern(env, svc_name, [pat]):
                    env_services.setdefault(env, [])
                    if svc_name not in env_services[env]:
                        env_services[env].append(svc_name)
                    matched_patterns.add(pat)

    dead_patterns = [p for p in patterns if p not in matched_patterns]
    return env_services, dead_patterns


def _read_manifest_context(
    container: Container,
    workspace_root: Path | None,
    patterns: list[str],
) -> tuple[str, list[str]] | None:
    """Read session_prefix and service names from the manifest.

    Tries to derive a candidate env from a concrete env segment in the
    patterns; falls back to any running tmux session.  Returns
    ``(prefix, service_names)`` or ``None`` on failure.
    """
    candidate_env: str | None = None

    # Prefer a concrete env from a pattern
    for pat in patterns:
        seg = pat if "/" not in pat else pat.split("/", 1)[0]
        if not _is_glob(seg):
            candidate_env = seg
            break

    # Fall back to any running session
    if candidate_env is None:
        sessions = container.tmux.list_sessions()
        for sess in sessions:
            if "-" in sess:
                candidate_env = sess.split("-", 1)[1]
                break

    if candidate_env is None:
        return None

    try:
        ctx = container.env_context_builder.build(candidate_env, workspace_root=workspace_root)
        return (
            ctx.manifest.session_prefix,
            [svc.name for svc in ctx.manifest.services],
        )
    except (ManifestError, OSError, OrchestratorError):
        return None


def main(argv: list[str]) -> int:
    """Parse ``[action, *rest]`` and dispatch to ``OrchestratorService`` or ``LogService``.

    Returns an integer exit code (0 = success, non-zero = failure).
    Catches ``OrchestratorError`` and ``ManifestError`` at this boundary;
    everything else propagates.
    """
    if not argv:
        print(
            f"usage: orchestrate <action> [<pattern>...]\n  action: {', '.join(_ACTIONS)}",
            file=sys.stderr,
        )
        return 2

    action, *rest = argv

    if action not in _ACTIONS:
        print(
            f"orchestrate: unknown action '{action}' (expected one of: {', '.join(_ACTIONS)})",
            file=sys.stderr,
        )
        return 2

    container = Container()

    ws_dir = os.environ.get("WINTER_WORKSPACE_DIR")
    workspace_root = Path(ws_dir) if ws_dir else None

    # ------------------------------------------------------------------
    # up / down: single env positional, behavior unchanged
    # ------------------------------------------------------------------
    if action in ("up", "down"):
        if len(rest) != 1:
            print(
                f"usage: orchestrate {action} <env>",
                file=sys.stderr,
            )
            return 2
        env = rest[0]
        try:
            ctx = container.env_context_builder.build(env, workspace_root=workspace_root)
        except ManifestError as exc:
            print(f"orchestrate: env '{env}': manifest error: {exc}", file=sys.stderr)
            return 1
        except (OSError, OrchestratorError) as exc:
            print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
            return 1
        try:
            if action == "up":
                return container.orchestrator.up(ctx)
            else:
                return container.orchestrator.down(ctx)
        except OrchestratorError as exc:
            print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
            return 1

    # ------------------------------------------------------------------
    # status: 0 or more patterns
    # ------------------------------------------------------------------
    if action == "status":
        patterns = rest
        json_output = os.environ.get("WINTER_STATUS_JSON") == "1"

        if not patterns:
            # 0 patterns → status all running envs
            sessions = container.tmux.list_sessions()
            if not sessions:
                print("No running sessions.")
                return 0

            # Derive prefix from the first session to enumerate envs
            candidate_session = sessions[0]
            candidate_env_name = candidate_session.split("-", 1)[1] if "-" in candidate_session else candidate_session
            try:
                seed_ctx = container.env_context_builder.build(candidate_env_name, workspace_root=workspace_root)
            except (ManifestError, OSError, OrchestratorError) as exc:
                print(f"orchestrate: could not read manifest: {exc}", file=sys.stderr)
                return 1

            prefix = seed_ctx.manifest.session_prefix
            envs = running_envs(container.tmux, prefix)

            rc = 0
            for env in envs:
                try:
                    ctx = container.env_context_builder.build(env, workspace_root=workspace_root)
                except (ManifestError, OSError, OrchestratorError) as exc:
                    print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                    rc = 1
                    continue
                try:
                    result = container.orchestrator.status(ctx, json_output=json_output)
                    if result != 0:
                        rc = result
                except OrchestratorError as exc:
                    print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                    rc = 1
            return rc

        # N patterns → expand to (env, service) pairs
        manifest_info = _read_manifest_context(container, workspace_root, patterns)
        if manifest_info is None:
            for pat in patterns:
                print(
                    f"orchestrate: status: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        prefix, services_list = manifest_info
        env_services, dead_patterns = _expand_patterns(container, patterns, workspace_root, services_list, prefix)

        if dead_patterns:
            for pat in dead_patterns:
                print(
                    f"orchestrate: status: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        rc = 0
        for env, svc_names in env_services.items():
            try:
                ctx = container.env_context_builder.build(env, workspace_root=workspace_root)
            except (ManifestError, OSError, OrchestratorError) as exc:
                print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                rc = 1
                continue
            try:
                result = container.orchestrator.status(ctx, services=tuple(svc_names), json_output=json_output)
                if result != 0:
                    rc = result
            except OrchestratorError as exc:
                print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                rc = 1
        return rc

    # ------------------------------------------------------------------
    # restart: 1 or more patterns required
    # ------------------------------------------------------------------
    if action == "restart":
        if not rest:
            print(
                "orchestrate: restart requires at least one pattern",
                file=sys.stderr,
            )
            return 1

        patterns = rest
        manifest_info = _read_manifest_context(container, workspace_root, patterns)
        if manifest_info is None:
            for pat in patterns:
                print(
                    f"orchestrate: restart: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        prefix, services_list = manifest_info
        env_services, dead_patterns = _expand_patterns(container, patterns, workspace_root, services_list, prefix)

        if dead_patterns:
            for pat in dead_patterns:
                print(
                    f"orchestrate: restart: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        rc = 0
        for env, svc_names in env_services.items():
            try:
                ctx = container.env_context_builder.build(env, workspace_root=workspace_root)
            except (ManifestError, OSError, OrchestratorError) as exc:
                print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                rc = 1
                continue
            for svc_name in svc_names:
                try:
                    result = container.orchestrator.restart(ctx, svc_name)
                    if result != 0:
                        rc = result
                except OrchestratorError as exc:
                    print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                    rc = 1
        return rc

    # ------------------------------------------------------------------
    # logs: 1 or more patterns required
    # ------------------------------------------------------------------
    if action == "logs":
        if not rest:
            print(
                "orchestrate: logs requires at least one pattern",
                file=sys.stderr,
            )
            return 1

        patterns = rest
        manifest_info = _read_manifest_context(container, workspace_root, patterns)
        if manifest_info is None:
            for pat in patterns:
                print(
                    f"orchestrate: logs: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        prefix, services_list = manifest_info
        env_services, dead_patterns = _expand_patterns(container, patterns, workspace_root, services_list, prefix)

        if dead_patterns:
            for pat in dead_patterns:
                print(
                    f"orchestrate: logs: pattern '{pat}' matched no services",
                    file=sys.stderr,
                )
            return 1

        follow = os.environ.get("WINTER_LOG_FOLLOW") == "1"
        tail = _parse_tail(os.environ.get("WINTER_LOG_TAIL", "all"))
        since = os.environ.get("WINTER_LOG_SINCE", "")
        until = os.environ.get("WINTER_LOG_UNTIL", "")
        timestamps = os.environ.get("WINTER_LOG_TIMESTAMPS") == "1"

        rc = 0

        if follow:
            pairs: list[tuple[EnvContext, LogQuery]] = []
            for env, svc_names in env_services.items():
                try:
                    ctx = container.env_context_builder.build(env, workspace_root=workspace_root)
                except (ManifestError, OSError, OrchestratorError) as exc:
                    print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                    rc = 1
                    continue
                pairs.append(
                    (
                        ctx,
                        LogQuery(
                            services=tuple(svc_names),
                            follow=True,
                            tail=tail,
                            since=since,
                            until=until,
                            timestamps=timestamps,
                        ),
                    )
                )
            if not pairs:
                return rc or 1
            try:
                result = container.log_service.follow_streams(pairs)
            except OrchestratorError as exc:
                print(f"orchestrate: follow_streams: {exc}", file=sys.stderr)
                return 1
            return result if result != 0 else rc

        for env, svc_names in env_services.items():
            try:
                ctx = container.env_context_builder.build(env, workspace_root=workspace_root)
            except (ManifestError, OSError, OrchestratorError) as exc:
                print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                rc = 1
                continue
            query = LogQuery(
                services=tuple(svc_names),
                follow=follow,
                tail=tail,
                since=since,
                until=until,
                timestamps=timestamps,
            )
            try:
                result = container.log_service.logs(ctx, query)
                if result != 0:
                    rc = result
            except OrchestratorError as exc:
                print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
                rc = 1
        return rc

    # Should be unreachable — already guarded above.
    print(f"orchestrate: internal error: unhandled action '{action}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
