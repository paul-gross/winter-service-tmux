"""Name-addressed entrypoint door — conforms to the winter ``orchestrate_services`` contract.

Winter always invokes this as exactly::

    <entrypoint> <action> <env>

``<action>`` is one of ``up``, ``down``, ``status``, ``restart``, ``logs``.
``<env>`` is the feature-env name (e.g. ``alpha``).

Per-action parameters arrive via ``WINTER_*`` environment variables:
- ``WINTER_WORKSPACE_DIR`` — absolute path to the workspace root (always set).
- ``WINTER_EXT_DIR``       — path to the extension clone (always set).
- ``WINTER_EXT_PREFIX``    — resolved symlink prefix (always set).
- ``WINTER_SERVICE_NAME``  — service name to bounce (set for ``restart``).
- ``WINTER_LOG_SERVICES``  — space-joined service names; empty = all (set for ``logs``).
- ``WINTER_LOG_FOLLOW``    — ``1`` = follow, ``0`` = backlog-only (set for ``logs``).
- ``WINTER_LOG_TAIL``      — positive int or ``all`` (set for ``logs``).
- ``WINTER_LOG_SINCE``     — RFC3339 timestamp lower bound; empty if unset (set for ``logs``).
- ``WINTER_LOG_UNTIL``     — RFC3339 timestamp upper bound; empty if unset (set for ``logs``).
- ``WINTER_LOG_TIMESTAMPS``— ``1`` = timestamps requested (set for ``logs``).

Exit-code passthrough: the orchestrator's return value becomes this process's
exit code, as required by the contract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from service_manifest.modules.manifest.errors import ManifestError
from service_orchestrator.container import Container
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.log_query import LogQuery

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


def main(argv: list[str]) -> int:
    """Parse ``[action, env]`` and dispatch to ``OrchestratorService`` or ``LogService``.

    Returns an integer exit code (0 = success, non-zero = failure).
    Catches ``OrchestratorError`` and ``ManifestError`` at this boundary;
    everything else propagates.
    """
    if len(argv) != 2:
        print(
            f"usage: orchestrate <action> <env>\n  action: {', '.join(_ACTIONS)}",
            file=sys.stderr,
        )
        return 2

    action, env = argv

    if action not in _ACTIONS:
        print(
            f"orchestrate: unknown action '{action}' (expected one of: {', '.join(_ACTIONS)})",
            file=sys.stderr,
        )
        return 2

    container = Container()
    builder = container.env_context_builder

    try:
        # Workspace root comes from WINTER_WORKSPACE_DIR (always set by winter).
        ws_dir = os.environ.get("WINTER_WORKSPACE_DIR")
        workspace_root = Path(ws_dir) if ws_dir else None
        ctx = builder.build(env, workspace_root=workspace_root)
    except ManifestError as exc:
        print(f"orchestrate: env '{env}': manifest error: {exc}", file=sys.stderr)
        return 1
    except (OSError, OrchestratorError) as exc:
        print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
        return 1

    try:
        if action == "up":
            return container.orchestrator.up(ctx)
        elif action == "down":
            return container.orchestrator.down(ctx)
        elif action == "status":
            return container.orchestrator.status(ctx)
        elif action == "restart":
            service_name = os.environ.get("WINTER_SERVICE_NAME", "")
            if not service_name:
                print(
                    "orchestrate: restart requires WINTER_SERVICE_NAME to be set",
                    file=sys.stderr,
                )
                return 1
            return container.orchestrator.restart(ctx, service_name)
        elif action == "logs":
            services_raw = os.environ.get("WINTER_LOG_SERVICES", "")
            services = tuple(services_raw.split()) if services_raw.strip() else ()
            follow = os.environ.get("WINTER_LOG_FOLLOW") == "1"
            tail = _parse_tail(os.environ.get("WINTER_LOG_TAIL", "all"))
            since = os.environ.get("WINTER_LOG_SINCE", "")
            until = os.environ.get("WINTER_LOG_UNTIL", "")
            timestamps = os.environ.get("WINTER_LOG_TIMESTAMPS") == "1"
            query = LogQuery(
                services=services,
                follow=follow,
                tail=tail,
                since=since,
                until=until,
                timestamps=timestamps,
            )
            return container.log_service.logs(ctx, query)
        else:
            # Should be unreachable — already guarded above.
            print(f"orchestrate: internal error: unhandled action '{action}'", file=sys.stderr)
            return 2
    except OrchestratorError as exc:
        print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
