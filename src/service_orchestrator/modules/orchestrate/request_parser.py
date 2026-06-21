"""Argv parser for the winter orchestrate door.

Parses ``[action, *rest]`` into a typed ``OrchestratorRequest`` or a
``ParseError`` carrying the pre-formatted stderr message and exit code.
No flag parsing is performed — dash-leading tokens are literal patterns.
"""

from __future__ import annotations

from dataclasses import dataclass

_ACTIONS = ("up", "down", "status", "restart", "logs")


@dataclass(frozen=True)
class OrchestratorRequest:
    """Parsed, validated orchestrator request.

    Attributes:
        action:   One of the five supported actions.
        env:      Set for ``up``/``down``; ``None`` for pattern-based actions.
        patterns: Set for ``status``/``restart``/``logs``; empty list for ``up``/``down``.
    """

    action: str
    env: str | None
    patterns: list[str]


@dataclass(frozen=True)
class ParseError:
    """A validation failure from ``parse_request``.

    Attributes:
        message:   Already-formatted line(s) to print to stderr.
        exit_code: The process exit code to return (1 or 2).
    """

    message: str
    exit_code: int


def parse_request(argv: list[str]) -> OrchestratorRequest | ParseError:
    """Parse and validate ``argv`` into an ``OrchestratorRequest`` or ``ParseError``.

    Exact error strings and exit codes are preserved verbatim from ``cli.py``
    so that existing tests keep passing unedited.

    No flag parsing: dash-leading tokens are treated as literal patterns.
    """
    if not argv:
        return ParseError(
            message=f"usage: orchestrate <action> [<pattern>...]\n  action: {', '.join(_ACTIONS)}",
            exit_code=2,
        )

    action, *rest = argv

    if action not in _ACTIONS:
        return ParseError(
            message=f"orchestrate: unknown action '{action}' (expected one of: {', '.join(_ACTIONS)})",
            exit_code=2,
        )

    if action in ("up", "down"):
        if len(rest) != 1:
            return ParseError(
                message=f"usage: orchestrate {action} <env>",
                exit_code=2,
            )
        return OrchestratorRequest(action=action, env=rest[0], patterns=[])

    if action == "restart":
        if not rest:
            return ParseError(
                message="orchestrate: restart requires at least one pattern",
                exit_code=1,
            )
        return OrchestratorRequest(action=action, env=None, patterns=list(rest))

    if action == "logs":
        if not rest:
            return ParseError(
                message="orchestrate: logs requires at least one pattern",
                exit_code=1,
            )
        return OrchestratorRequest(action=action, env=None, patterns=list(rest))

    # action == "status": 0 or more patterns
    return OrchestratorRequest(action=action, env=None, patterns=list(rest))
