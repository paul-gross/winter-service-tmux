"""LogQuery value object — parameters for the ``logs`` action.

Constructed by ``DispatchService`` (``logs_backlog`` / ``logs_follow``) from
``WINTER_LOG_*`` environment variables and passed to ``LogService.logs``.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO


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

    @classmethod
    def from_env(cls, services: tuple[str, ...], env: Mapping[str, str]) -> LogQuery:
        """Construct a ``LogQuery`` from a ``WINTER_LOG_*`` environment mapping.

        *env* is typically ``os.environ`` but any mapping works, making this
        unit-testable without monkeypatching global state.
        """
        follow = env.get("WINTER_LOG_FOLLOW") == "1"
        tail = parse_tail(env.get("WINTER_LOG_TAIL", "all"))
        since = env.get("WINTER_LOG_SINCE", "")
        until = env.get("WINTER_LOG_UNTIL", "")
        timestamps = env.get("WINTER_LOG_TIMESTAMPS") == "1"
        return cls(
            services=services,
            follow=follow,
            tail=tail,
            since=since,
            until=until,
            timestamps=timestamps,
        )


def parse_tail(raw: str, *, err_sink: IO[str] | None = None) -> int | None:
    """Parse ``WINTER_LOG_TAIL`` into an int or None.

    ``"all"`` or empty string → ``None`` (return all events).
    A positive integer string → ``int``.
    Anything else → ``None`` with a warning written to *err_sink*
    (defaults to ``sys.stderr``).
    """
    if err_sink is None:
        err_sink = sys.stderr
    raw = raw.strip()
    if not raw or raw == "all":
        return None
    try:
        return int(raw)
    except ValueError:
        print(
            f"orchestrate: WINTER_LOG_TAIL '{raw}' is not a valid integer or 'all'; treating as 'all'",
            file=err_sink,
        )
        return None
