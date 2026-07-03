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

``service_status`` is a SEPARATE, data-returning method on the same seam: it
runs ``winter service status <pattern> --json``, parses the resulting status
document, and returns the matched dependency's ``{state, health}`` — used by
``OrchestratorService.up`` to gate a ``depends_on`` service on another
service's readiness (same-env tmux service or a cross-provider service, e.g. a
docker workspace singleton). Unlike ``service``, it captures output and never
lets a child's stdout/stderr reach the terminal — it is consumed
programmatically, not rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class WinterCliUnavailableError(Exception):
    """Raised by ``service_status`` when the ``winter`` CLI cannot be invoked at all.

    Distinct from a ``None`` return: ``None`` means the subprocess RAN but
    reported nothing usable (non-zero exit, unparseable stdout, no matching
    service) — a state that may resolve on a later poll, so the caller keeps
    polling. This exception means the CLI itself could not be launched (e.g.
    the ``winter`` binary is missing from ``PATH``, or another OS-level exec
    failure) — a condition that will never resolve no matter how many times
    the caller polls, so callers should fail fast with a clear message instead
    of exhausting their poll timeout.
    """


@dataclass(frozen=True)
class DependencyStatus:
    """The ``{state, health}`` of one ``depends_on`` dependency.

    Mirrors the two enum fields of a single service entry in winter's
    ``status`` wire contract (``state``: ``"running"``/``"stopped"``/
    ``"unknown"``; ``health``: ``"healthy"``/``"unhealthy"``/``"unknown"``).
    """

    state: str
    health: str


class IWinterCli(Protocol):
    """Delegate to the workspace-level ``winter`` CLI."""

    def service(self, args: list[str]) -> int:
        """Run ``winter service <args...>`` with stdio passed through; return its exit code."""
        ...

    def service_status(self, pattern: str) -> DependencyStatus | None:
        """Run ``winter service status <pattern> --json`` and return the matched dependency's status.

        *pattern* must be scope-qualified (``<scope>/<service>``, e.g.
        ``"alpha/builder"`` or ``"workspace/db"``). Returns ``None`` when the
        subprocess runs but exits non-zero, its stdout cannot be parsed as a
        conformant status document, or the document contains no service
        matching *pattern* — the caller treats ``None`` as "not yet ready" and
        keeps polling. Raises ``WinterCliUnavailableError`` when the ``winter``
        CLI cannot be invoked at all (e.g. missing from ``PATH``) — a
        condition polling will never resolve, so the caller should fail fast
        instead.
        """
        ...
