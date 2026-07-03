"""Cross-cutting seam for delegating to the workspace-level ``winter`` CLI.

The env-root door (``env_cli``) shells out to ``winter service <action> <env>``
so lifecycle fans out across *every* bound service provider (capability
dispatch), not just this tmux orchestrator. That call is a **transparent stdio
passthrough** — the child's stdout/stderr must reach the user's terminal
unbuffered (winter's interactive status tables, ``logs -f``, the human
renderer), so this seam deliberately does *not* ``capture_output`` or wrap
non-zero exits into an error type the way ``winter-harness:/architecture/subprocess.md``
prescribes for data-returning adapters. Its sole purpose is to keep the raw
``subprocess`` call under ``internal/`` and let the door's tests inject a fake
instead of monkeypatching stdlib.
"""

from __future__ import annotations

from typing import Protocol


class IWinterCli(Protocol):
    """Delegate to the workspace-level ``winter`` CLI."""

    def service(self, args: list[str]) -> int:
        """Run ``winter service <args...>`` with stdio passed through; return its exit code."""
        ...
