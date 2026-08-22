"""Environment-source seam used by lifecycle resolution.

The orchestrator needs the same scope and env-file values that a pane will see,
including on actions where winter core intentionally does not inject scope
bands into the provider process.  The production implementation lives under
``internal/``; this protocol keeps the orchestrator free of subprocess calls
and gives tests a small, explicit seam to replace.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol


class IEnvironmentSource(Protocol):
    """Resolve a scope baseline and a shell-sourced env file."""

    def scope_environment(
        self,
        scope: str,
        *,
        cwd: Path,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        """Return *base* after applying the canonical ``winter env`` scope."""
        ...

    def env_file_environment(
        self,
        path: Path,
        *,
        cwd: Path,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        """Return *base* after sourcing *path* with shell semantics."""
        ...


class ProcessEnvironmentSource:
    """Compatibility source for direct unit-level construction.

    The application composition root always injects the subprocess-backed
    implementation.  Keeping this no-I/O default preserves the historical
    ``OrchestratorService`` constructor contract for embedders and focused
    tests; it still supplies the provider process environment as the baseline.
    """

    def scope_environment(
        self,
        scope: str,
        *,
        cwd: Path,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        del scope, cwd
        return dict(base)

    def env_file_environment(
        self,
        path: Path,
        *,
        cwd: Path,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        del path, cwd
        return dict(base)


def _conforms_process_environment_source(x: ProcessEnvironmentSource) -> IEnvironmentSource:
    """Typecheck-time protocol assertion."""
    return x
