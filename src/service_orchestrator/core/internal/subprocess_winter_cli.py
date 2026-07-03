"""``winter service`` adapters.  The raw ``subprocess`` calls are confined here.

``service`` is a *transparent stdio passthrough* — it hands the child's
stdout/stderr straight to the terminal and returns its exit code verbatim. See
``service_orchestrator.core.winter_cli`` for why that intentionally diverges
from ``architecture/subprocess.md``.

``service_status`` is the data-returning counterpart: it captures
``winter service status <pattern> --json``'s stdout, parses the status
document, and returns the one matched dependency's ``{state, health}`` —
never letting the child's stdio reach the terminal.
"""

from __future__ import annotations

import json
import subprocess
import sys

from service_orchestrator.core.winter_cli import DependencyStatus, IWinterCli, WinterCliUnavailableError


class SubprocessWinterCli:
    """Delegate to ``winter service`` — passthrough ``service``, capturing ``service_status``."""

    def service(self, args: list[str]) -> int:
        try:
            return subprocess.run(["winter", "service", *args]).returncode
        except FileNotFoundError:
            print(
                "error: 'winter' not found on PATH — the env-root scripts delegate to "
                "`winter service`; install the winter CLI or run it directly.",
                file=sys.stderr,
            )
            return 127

    def service_status(self, pattern: str) -> DependencyStatus | None:
        try:
            completed = subprocess.run(
                ["winter", "service", "status", pattern, "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
        except (FileNotFoundError, OSError) as exc:
            # The CLI itself could not be launched (missing binary / exec
            # failure) — this will never resolve on a later poll, so fail
            # fast rather than let the caller treat it as "not ready yet".
            raise WinterCliUnavailableError(f"'winter' CLI unavailable while polling '{pattern}': {exc}") from exc
        if completed.returncode != 0:
            return None
        try:
            doc = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        return _extract_dependency_status(doc, pattern)


def _extract_dependency_status(doc: object, pattern: str) -> DependencyStatus | None:
    """Find *pattern*'s ``{state, health}`` in a parsed status document.

    *pattern* must be scope-qualified (``<scope>/<service>``); the scope
    segment is matched against a document env's ``env`` field and the service
    segment against a service's ``name`` field. Returns ``None`` on any shape
    mismatch (not a dict, missing/malformed ``envs``) or when no service
    matches.
    """
    if not isinstance(doc, dict):
        return None
    envs = doc.get("envs")
    if not isinstance(envs, list):
        return None
    scope, _, svc_name = pattern.partition("/")
    if not svc_name:
        return None
    for env_doc in envs:
        if not isinstance(env_doc, dict) or env_doc.get("env") != scope:
            continue
        services = env_doc.get("services")
        if not isinstance(services, list):
            continue
        for svc in services:
            if isinstance(svc, dict) and svc.get("name") == svc_name:
                state = svc.get("state")
                health = svc.get("health")
                return DependencyStatus(
                    state=state if isinstance(state, str) else "unknown",
                    health=health if isinstance(health, str) else "unknown",
                )
    return None


def _conforms_subprocess_winter_cli(x: SubprocessWinterCli) -> IWinterCli:
    return x
