"""LogQuery value object — parameters for the ``logs`` action.

Constructed by ``cli.py`` from ``WINTER_LOG_*`` environment variables and
passed to ``LogService.logs``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LogQuery:
    """Immutable query parameters for the ``logs`` action.

    Attributes:
        services: Tuple of requested service names.  Empty tuple means all.
        follow: When True, stream live output after emitting the backlog.
        tail: Keep only the last *tail* events of the merged stream.
            ``None`` means return all events.
        since: RFC3339 absolute timestamp lower bound; empty string if unset.
            Server-side filter: events with ``ts < since`` are dropped.
            Events without a ``ts`` (pane-mode) are always kept.
        until: RFC3339 absolute timestamp upper bound; empty string if unset.
            Server-side filter: events with ``ts > until`` are dropped.
            Events without a ``ts`` (pane-mode) are always kept.
        timestamps: When True, per-line timestamps were requested by the user.
            File-mode events already carry ``ts``; rendering is winter's
            responsibility.  No orchestrator-side handling needed.
    """

    services: tuple[str, ...]
    follow: bool
    tail: int | None
    since: str
    until: str
    timestamps: bool
