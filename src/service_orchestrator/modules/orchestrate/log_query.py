"""LogQuery value object — parameters for the ``logs`` action.

Render options arrive on argv (parsed by ``parse_log_args`` into a
``LogRenderOptions``); ``DispatchService`` (``logs_backlog`` / ``logs_follow``)
combines them with each env's service names via ``LogQuery.from_render`` and
passes the result to ``LogService.logs``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import IO


@dataclass(frozen=True)
class LogRenderOptions:
    """Per-invocation render options for the ``logs`` action, parsed from argv.

    These are the service-agnostic flags winter appends after the positional
    ``<pattern...>`` tokens; ``services`` is folded in per-env later.

    Attributes:
        follow: When True, stream live output after emitting the backlog.
        tail: Keep only the last *tail* events; ``None`` means all.
        since: RFC3339 absolute lower bound; empty string if unset.
        until: RFC3339 absolute upper bound; empty string if unset.
        timestamps: When True, per-line timestamps were requested.
    """

    follow: bool
    tail: int | None
    since: str
    until: str
    timestamps: bool


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
    def from_render(cls, services: tuple[str, ...], render: LogRenderOptions) -> LogQuery:
        """Combine per-env *services* with the argv-parsed *render* options."""
        return cls(
            services=services,
            follow=render.follow,
            tail=render.tail,
            since=render.since,
            until=render.until,
            timestamps=render.timestamps,
        )


def parse_log_args(tokens: list[str]) -> tuple[list[str], LogRenderOptions]:
    """Split the ``logs`` action's argv ``tokens`` into patterns + render options.

    Mirrors ``winter service logs``' own flag surface, which winter appends
    after the positional ``<pattern...>`` tokens::

        <pattern...> [-f|--follow] [-n|--tail <N|all>] \\
          [--since <rfc3339>] [--until <rfc3339>] [-t|--timestamps]

    ``--since``/``--until`` carry winter's already-resolved RFC3339 values and
    are consumed as-is (no duration re-parsing). ``--tail`` carries the resolved
    count string (``N`` or ``all``). Any non-flag token is a positional pattern.

    This is a thin contract parser, not a general getopt: it relies on winter's
    emission guarantees — selection patterns never lead with ``-``, and a value
    flag (``-n``/``--tail``, ``--since``, ``--until``) is always followed by its
    value. Do not "harden" it past those guarantees without changing the
    producer contract too.
    """
    patterns: list[str] = []
    follow = False
    tail_raw = "all"
    since = ""
    until = ""
    timestamps = False

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("-f", "--follow"):
            follow = True
        elif tok in ("-t", "--timestamps"):
            timestamps = True
        elif tok in ("-n", "--tail"):
            i += 1
            tail_raw = tokens[i] if i < n else "all"
        elif tok == "--since":
            i += 1
            since = tokens[i] if i < n else ""
        elif tok == "--until":
            i += 1
            until = tokens[i] if i < n else ""
        else:
            patterns.append(tok)
        i += 1

    return patterns, LogRenderOptions(
        follow=follow,
        tail=parse_tail(tail_raw),
        since=since,
        until=until,
        timestamps=timestamps,
    )


def parse_tail(raw: str, *, err_sink: IO[str] | None = None) -> int | None:
    """Parse a ``--tail`` count string into an int or None.

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
            f"orchestrate: --tail '{raw}' is not a valid integer or 'all'; treating as 'all'",
            file=err_sink,
        )
        return None
