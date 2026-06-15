"""Factory that wraps ``subprocess.CompletedProcess`` into ``TmuxError``.

Injected into ``CliTmuxRepository`` so every tmux wrap site passes only a
high-level *message*; the factory extracts ``cmd_args``, ``exit_code``, and
``stderr`` from the completed process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from service_orchestrator.modules.orchestrate.errors import TmuxError


class TmuxErrorFactory:
    """Wraps a failed ``subprocess.CompletedProcess`` into a ``TmuxError``.

    ``from_subprocess`` is the single wrap method.  It extracts structured
    fields from *completed* so callers don't repeat the extraction at every
    wrap site.
    """

    @staticmethod
    def from_subprocess(
        completed: subprocess.CompletedProcess,  # type: ignore[type-arg]
        message: str,
        *,
        cwd: Path | None = None,
    ) -> TmuxError:
        """Build a ``TmuxError`` from *completed*.

        *message* is the high-level description (e.g. ``"new-session failed"``).
        The factory appends ``cwd``, ``exit_code``, and ``stderr`` as structured
        fields.
        """
        cmd_args: list[str] = list(completed.args) if completed.args else []
        detail = f"{message} (exit {completed.returncode})"
        if cwd is not None:
            detail += f" [cwd={cwd}]"
        return TmuxError(
            detail,
            cmd_args=cmd_args,
            exit_code=completed.returncode,
            stderr=completed.stderr or "",
        )
