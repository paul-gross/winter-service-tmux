"""POSIX-shell environment source used by the tmux orchestrator."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from service_orchestrator.modules.orchestrate.environment_source import IEnvironmentSource
from service_orchestrator.modules.orchestrate.errors import OrchestratorError

_SHELL = shutil.which("sh", path=os.defpath)

_SCOPE_SCRIPT = """
set -eu
scope_output=$(winter env "$1") || exit $?
eval "$scope_output"
command -p env -0
"""

_ENV_FILE_SCRIPT = """
# Keep normal dot-source behavior for intermediate failures, but preserve the file's final status.
set -a
. "$1"
env_file_status=$?
set +a
[ "$env_file_status" -eq 0 ] || exit "$env_file_status"
command -p env -0
"""


class SubprocessEnvironmentSource:
    """Evaluate scope and env-file sources in the same POSIX shell model as panes."""

    def scope_environment(
        self,
        scope: str,
        *,
        cwd: Path,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        return self._run(_SCOPE_SCRIPT, scope, cwd=cwd, base=base, description=f"winter env {scope}")

    def env_file_environment(
        self,
        path: Path,
        *,
        cwd: Path,
        base: Mapping[str, str],
    ) -> dict[str, str]:
        source_path = path if path.is_absolute() else cwd / path
        if not source_path.is_file():
            raise OrchestratorError(f"could not source {source_path}: file does not exist")
        return self._run(
            _ENV_FILE_SCRIPT,
            str(source_path),
            cwd=cwd,
            base=base,
            description=f"source {source_path}",
        )

    @staticmethod
    def _run(
        script: str,
        argument: str,
        *,
        cwd: Path,
        base: Mapping[str, str],
        description: str,
    ) -> dict[str, str]:
        if _SHELL is None:
            raise OrchestratorError(f"could not {description}: POSIX shell not found")
        try:
            completed = subprocess.run(
                [_SHELL, "-c", script, "winter-service-tmux-env", argument],
                cwd=cwd,
                env=dict(base),
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise OrchestratorError(f"could not {description}: {exc}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise OrchestratorError(f"could not {description} (exit {completed.returncode}){suffix}")

        return _parse_environment(completed.stdout, description)


def _parse_environment(raw: bytes, description: str) -> dict[str, str]:
    """Decode ``env -0`` output without losing spaces or shell metacharacters."""
    values: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"=")
        if not separator:
            raise OrchestratorError(f"could not {description}: malformed environment output")
        try:
            key_text = key.decode("utf-8")
            value_text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OrchestratorError(f"could not {description}: environment output is not UTF-8") from exc
        values[key_text] = value_text
    return values


def _conforms_subprocess_environment_source(x: SubprocessEnvironmentSource) -> IEnvironmentSource:
    """Typecheck-time protocol assertion."""
    return x
