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

_ACTIONS = ("up", "down", "status", "restart", "logs")


def main(argv: list[str]) -> int:
    """Parse ``[action, env]`` and dispatch to ``OrchestratorService``.

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

    if action == "logs":
        print(
            "logs: unsupported action (not implemented until #3)",
            file=sys.stderr,
        )
        return 1

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

    orchestrator = container.orchestrator

    try:
        if action == "up":
            return orchestrator.up(ctx)
        elif action == "down":
            return orchestrator.down(ctx)
        elif action == "status":
            return orchestrator.status(ctx)
        elif action == "restart":
            service_name = os.environ.get("WINTER_SERVICE_NAME", "")
            if not service_name:
                print(
                    "orchestrate: restart requires WINTER_SERVICE_NAME to be set",
                    file=sys.stderr,
                )
                return 1
            return orchestrator.restart(ctx, service_name)
        else:
            # Should be unreachable — already guarded above.
            print(f"orchestrate: internal error: unhandled action '{action}'", file=sys.stderr)
            return 2
    except OrchestratorError as exc:
        print(f"orchestrate: env '{env}': {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
