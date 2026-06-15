"""Domain error hierarchy for the orchestrate module.

``OrchestratorError`` is the base type callers catch to handle any failure from
the orchestrator.  ``TmuxError`` narrows it to tmux-command failures and carries
the structured fields needed to surface a useful error message.
"""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base class for all orchestrator failures."""


class TmuxError(OrchestratorError):
    """Raised when a ``tmux`` subprocess exits non-zero or cannot be launched.

    Carries structured fields rather than a concatenated message so callers can
    render them independently (e.g. show ``cmd_args`` separately from
    ``stderr``).
    """

    def __init__(
        self,
        message: str,
        *,
        cmd_args: list[str],
        exit_code: int,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.cmd_args = cmd_args
        self.exit_code = exit_code
        self.stderr = stderr
