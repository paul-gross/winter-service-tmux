"""``winter service`` passthrough adapter.  The raw ``subprocess`` call is confined here.

Unlike the other ``internal/`` subprocess adapters (which capture output and
wrap failures into a structured error), this one is a *transparent stdio
passthrough* — it hands the child's stdout/stderr straight to the terminal and
returns its exit code verbatim. See ``service_orchestrator.core.winter_cli`` for
why that intentionally diverges from ``architecture/subprocess.md``.
"""

from __future__ import annotations

import subprocess
import sys

from service_orchestrator.core.winter_cli import IWinterCli


class SubprocessWinterCli:
    """Delegate to ``winter service`` with the child's stdio passed straight through."""

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


def _conforms_subprocess_winter_cli(x: SubprocessWinterCli) -> IWinterCli:
    return x
