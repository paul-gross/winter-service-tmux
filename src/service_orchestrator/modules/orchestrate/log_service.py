"""Log read service — implements the ``logs`` action (backlog) and ``follow_streams``.

Reads persisted log files written by ``logwriter.py`` and emits NDJSON to
an injected output sink.  Pure helpers are free functions so they unit-test
without I/O.

Line format on disk: ``<RFC3339-Z ts>\\t<raw msg>\\n`` (TAB-separated).
Rotation: ``<svc>.log`` is newest; ``<svc>.log.1`` … ``<svc>.log.<N>`` are
older segments, higher number = older.

Follow mode:
  Handled by ``follow_streams`` (not by ``logs``).  The CLI always routes
  follow calls through ``follow_streams``; ``logs`` is backlog-only.

  ``follow_streams`` installs the follow clock once, then runs a single
  interleaved poll loop over a flat ``(ctx, service)`` unit list, keyed by
  ``(env, svc-name)`` to avoid same-service-name collisions across envs.
  Returns 130 on SIGINT, 0 on BrokenPipeError.

  Rotation/truncation during follow (v1): if a file shrinks below the
  tracked byte offset the offset is reset to 0, treating the file as a
  fresh segment.  Content rotated into numbered segments mid-follow is not
  replayed.

Per-service log modes (``LogMode``):
  FILE:   Read persisted segment files; events carry ``ts``.  Default.
  PANE:   Read the pane buffer via ``tmux capture-pane``; no ``ts`` (capture-
          pane provides no per-line timestamps).  Requires a running session;
          if capture fails, emits nothing for that service (one STDERR note).
          Follow best-effort: re-capture each tick and emit only new lines
          (lines not yet emitted, tracked by tail index).  Approximation —
          documented in comments below.
  MEMORY: Stub — emits nothing (future work).
"""

from __future__ import annotations

import json
import sys
from typing import IO

from service_manifest.modules.manifest.model import LogMode, Service
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.log_query import LogQuery
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_FOLLOW_POLL_INTERVAL = 0.25  # seconds between live-poll ticks


def parse_line(raw: str) -> tuple[str | None, str]:
    """Parse one persisted log line into ``(ts, msg)``.

    Lines are ``<RFC3339-Z ts>\\t<raw msg>``.  A line with no tab yields
    ``(None, raw)`` so the caller emits an event with no ``ts`` field.
    """
    idx = raw.find("\t")
    if idx == -1:
        return None, raw
    return raw[:idx], raw[idx + 1 :]


def build_event(ts: str | None, env: str, svc: str, msg: str) -> dict[str, str]:
    """Build an NDJSON event dict.

    Field ordering matches the wire contract: ``ts`` (when present), ``env``,
    ``svc``, ``msg``.  The ``ts`` key is omitted when *ts* is ``None`` (no
    per-line timestamp).
    """
    event: dict[str, str] = {}
    if ts is not None:
        event["ts"] = ts
    event["env"] = env
    event["svc"] = svc
    event["msg"] = msg
    return event


def merge_sorted(
    env: str,
    streams: list[tuple[str, list[tuple[str | None, str]]]],
) -> list[dict[str, str]]:
    """Merge per-service event streams into one stable time-sorted list.

    *streams* is a list of ``(service_name, [(ts_or_None, msg), …])`` pairs
    in manifest order.  Events without a timestamp sort before events that
    have one (sort key is empty string for None so they sort stably at the
    front of equal-ts groups).

    Within a single service the relative order of events is preserved.
    """
    all_events: list[dict[str, str]] = []
    for svc, pairs in streams:
        for ts, msg in pairs:
            all_events.append(build_event(ts, env, svc, msg))

    all_events.sort(key=lambda e: e.get("ts", ""))
    return all_events


def apply_tail(events: list[dict[str, str]], tail: int | None) -> list[dict[str, str]]:
    """Return the last *tail* events.  ``tail=None`` returns all events."""
    if tail is None:
        return events
    return events[-tail:]


def apply_time_filter(
    events: list[dict[str, str]],
    since: str,
    until: str,
) -> list[dict[str, str]]:
    """Filter *events* by ``since``/``until`` bounds (inclusive).

    Only events with a ``ts`` field are filtered; events without ``ts`` (e.g.
    pane-mode events) are always kept — winter's backstop mirrors this rule.

    Timestamp strings are RFC3339-UTC (``...Z``); all writer-emitted ``ts``
    values share the same microsecond-precision format so lexicographic string
    comparison is correct.  Empty string for either bound means no bound on
    that side.
    """
    if not since and not until:
        return events
    result: list[dict[str, str]] = []
    for event in events:
        ts = event.get("ts")
        if ts is None:
            # Pane-mode event — no timestamp, cannot be time-filtered; always kept.
            result.append(event)
            continue
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        result.append(event)
    return result


# ---------------------------------------------------------------------------
# LogService
# ---------------------------------------------------------------------------


class LogService:
    """Reads service logs and emits NDJSON.

    Injected with ``log_repo`` (the ILogRepository seam), ``tmux`` (the
    ITmuxRepository seam — used for pane-mode capture), ``follow_clock``
    (the IFollowClock seam), and an output ``sink`` (default ``sys.stdout``)
    so tests can capture output without capsys.
    """

    def __init__(
        self,
        log_repo: ILogRepository,
        follow_clock: IFollowClock,
        tmux: ITmuxRepository,
        sink: IO[str] | None = None,
    ) -> None:
        self._log_repo = log_repo
        self._follow_clock = follow_clock
        self._tmux = tmux
        self._sink: IO[str] = sink if sink is not None else sys.stdout

    def _gather_backlog(
        self,
        ctx: SessionContext,
        in_scope: list[Service],
        tail: int | None,
    ) -> tuple[list[dict[str, str]], dict[tuple[str, str], int]]:
        """Read each in-scope service's backlog for one env.

        Returns ``(events, file_seeds)`` where ``events`` is the per-env
        merge_sorted event list (build events + stable time-sort across all
        in-scope services) and ``file_seeds`` is keyed by
        ``(env, service-name)`` so callers can resume follow-mode reads
        without cross-env offset collisions.

        Note: ``since``/``until`` time-filtering and global ``tail`` trimming
        are NOT applied here — they are applied by the caller over the
        returned event list so the order of operations is preserved exactly
        as in the original backlog path.

        The PANE per-service tail trim (``lines[-tail:]``) IS applied here,
        matching the original gather-loop behaviour.
        """
        streams: list[tuple[str, list[tuple[str | None, str]]]] = []
        file_seeds: dict[tuple[str, str], int] = {}
        for svc in in_scope:
            pairs: list[tuple[str | None, str]] = []

            if svc.log == LogMode.FILE:
                active = self._log_repo.log_path(ctx.worktree_dir, svc.name)
                segments = self._log_repo.segment_files(ctx.worktree_dir, svc.name)
                for seg_path in segments:
                    if seg_path == active:
                        # Read the active file with an offset-returning read so
                        # the follow loop seeds from the byte boundary consumed
                        # here. Closed (rotated) segments use the plain reader.
                        raw_lines, file_seeds[(ctx.env, svc.name)] = self._log_repo.read_new_lines(active, 0)
                    else:
                        raw_lines = self._log_repo.read_lines(seg_path)
                    for raw in raw_lines:
                        if not raw:
                            continue
                        ts, msg = parse_line(raw)
                        pairs.append((ts, msg))

            elif svc.log == LogMode.PANE:
                target = f"{svc.target.window}.{svc.target.pane}"
                try:
                    captured = self._tmux.capture_pane(ctx.session, target)
                except Exception as exc:
                    print(
                        f"logs: pane capture failed for '{svc.name}' ({target}): {exc}",
                        file=sys.stderr,
                    )
                    captured = ""
                lines = [ln for ln in captured.splitlines() if ln]
                if tail is not None:
                    lines = lines[-tail:]
                for ln in lines:
                    pairs.append((None, ln))

            elif svc.log == LogMode.MEMORY:
                # MEMORY mode not yet implemented — emit a note to stderr so the
                # caller knows why the service is silent, rather than seeing
                # ambiguous empty output.
                print(
                    f"logs: memory-mode not yet implemented for '{svc.name}'; no output",
                    file=sys.stderr,
                )

            streams.append((svc.name, pairs))

        return merge_sorted(ctx.env, streams), file_seeds

    def logs(self, ctx: SessionContext, query: LogQuery) -> int:
        """Emit NDJSON backlog for the requested services (backlog-only).

        Follow mode is handled by ``follow_streams``; the CLI never calls
        ``logs`` with ``query.follow=True``.

        Steps:
          1. Determine in-scope services (filtered by ``query.services`` if set,
             else all manifest services in manifest order).
          2. For each service: read based on its LogMode (FILE reads segment
             files; PANE reads via capture-pane; MEMORY emits a STDERR note).
          3. Apply ``since``/``until`` filter (events without ``ts`` are kept).
          4. Merge across services, sort by ``ts``.
          5. Apply ``query.tail`` to the filtered set.
          6. Emit each event as a compact NDJSON line to ``self._sink``.
          Returns 0.
        """
        requested = set(query.services)
        in_scope = [s for s in ctx.services if s.name in requested] if requested else list(ctx.services)

        # --- backlog ---
        # _gather_backlog reads each service's events and returns the per-env
        # merge_sorted list plus file_seeds (used by follow_streams; discarded here).
        all_events, _ = self._gather_backlog(ctx, in_scope, query.tail)

        # Merge across services (build events + stable time-sort) → apply
        # since/until filter (order-preserving) → apply tail. Order of operations
        # ensures tail is the last N of the *filtered* set.
        #
        # v1 approximation: when both FILE and PANE services are in scope, the
        # global tail trims the mixed-mode merged set; PANE events (no ts) sort
        # before FILE events and may consume tail slots, making the effective
        # backlog for FILE-mode services smaller than requested.
        all_events = apply_time_filter(all_events, query.since, query.until)
        file_events = apply_tail(all_events, query.tail)

        try:
            for event in file_events:
                self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._sink.flush()
        except BrokenPipeError:
            return 0

        return 0

    def follow_streams(
        self,
        streams: list[tuple[SessionContext, LogQuery]],
    ) -> int:
        """Emit merged NDJSON for multiple ``(ctx, query)`` streams, then follow live.

        ``tail``, ``since``, and ``until`` are read from the per-pair
        ``LogQuery`` objects — the CLI sets them identically across all pairs.

        Backlog path:
          For each ``(ctx, query)`` pair, resolve in-scope services and call
          ``_gather_backlog``.  Concatenate all per-env event lists, re-sort
          by ``ts`` (cross-env merge; pane events with no ts sort first),
          apply ``since``/``until`` filter, apply ``tail``, and emit each
          event as NDJSON to ``self._sink``.

        Follow path:
          Install the follow clock once, then run a single interleaved poll
          loop over a flat ``(ctx, service)`` unit list — env order matches
          the ``streams`` argument order, service order matches the manifest
          order for each env.  FILE-mode units use byte-offset reads keyed by
          ``(ctx.env, svc.name)``; PANE-mode units use per-tick capture-diff
          keyed by the same tuple.

          Returns 130 on SIGINT, 0 on BrokenPipeError.
        """
        assert streams, "follow_streams requires at least one (ctx, query) pair"
        # tail/since/until are uniform across pairs (the CLI guarantees this).
        first_query = streams[0][1]
        tail = first_query.tail
        since = first_query.since
        until = first_query.until

        # --- build per-stream in_scope lists ---
        stream_units: list[tuple[SessionContext, list[Service]]] = []
        for ctx, query in streams:
            requested = set(query.services)
            in_scope = [s for s in ctx.services if s.name in requested] if requested else list(ctx.services)
            stream_units.append((ctx, in_scope))

        # --- merged backlog across all streams ---
        all_events: list[dict[str, str]] = []
        combined_file_seeds: dict[tuple[str, str], int] = {}

        for ctx, in_scope in stream_units:
            # Pass tail to _gather_backlog for the PANE per-service tail trim;
            # global tail is applied once below over the merged set.
            per_env_events, file_seeds = self._gather_backlog(ctx, in_scope, tail)
            all_events.extend(per_env_events)
            combined_file_seeds.update(file_seeds)

        # Cross-env merge: re-sort by ts (pane events with no ts sort to front).
        all_events.sort(key=lambda e: e.get("ts", ""))
        all_events = apply_time_filter(all_events, since, until)
        all_events = apply_tail(all_events, tail)

        try:
            for event in all_events:
                self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._sink.flush()
        except BrokenPipeError:
            return 0

        # --- live interleaved follow ---
        self._follow_clock.install()

        # Flat list of (ctx, service) units in streams order, manifest order per env.
        flat_units: list[tuple[SessionContext, Service]] = []
        for ctx, in_scope in stream_units:
            for svc in in_scope:
                flat_units.append((ctx, svc))

        # FILE-mode: seed byte offsets from combined backlog boundaries.
        offsets: dict[tuple[str, str], int] = {}
        for ctx, svc in flat_units:
            if svc.log == LogMode.FILE:
                key = (ctx.env, svc.name)
                offsets[key] = combined_file_seeds.get(key, 0)

        # PANE-mode: track emitted line counts per (env, svc).
        pane_emitted_counts: dict[tuple[str, str], int] = {}
        for ctx, svc in flat_units:
            if svc.log == LogMode.PANE:
                pane_emitted_counts[(ctx.env, svc.name)] = 0

        try:
            while not self._follow_clock.interrupted():
                for ctx, svc in flat_units:
                    if svc.log == LogMode.FILE:
                        active = self._log_repo.log_path(ctx.worktree_dir, svc.name)
                        key = (ctx.env, svc.name)
                        current_size = self._log_repo.file_size(active)

                        # Rotation/truncation: file shrank → reset to start of new segment.
                        if current_size < offsets[key]:
                            offsets[key] = 0

                        new_lines, new_offset = self._log_repo.read_new_lines(active, offsets[key])
                        offsets[key] = new_offset

                        for raw in new_lines:
                            if not raw:
                                continue
                            ts, msg = parse_line(raw)
                            # Apply since/until filter to each live line.
                            if ts is not None:
                                if since and ts < since:
                                    continue
                                if until and ts > until:
                                    continue
                            event = build_event(ts, ctx.env, svc.name, msg)
                            self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
                            self._sink.flush()

                    elif svc.log == LogMode.PANE:
                        # Best-effort pane follow: re-capture and emit only new lines.
                        target = f"{svc.target.window}.{svc.target.pane}"
                        try:
                            captured = self._tmux.capture_pane(ctx.session, target)
                        except Exception:
                            continue
                        all_lines = [ln for ln in captured.splitlines() if ln]
                        key = (ctx.env, svc.name)
                        prev_count = pane_emitted_counts[key]
                        new_lines_pane = all_lines[prev_count:]
                        pane_emitted_counts[key] = len(all_lines)
                        for ln in new_lines_pane:
                            event = build_event(None, ctx.env, svc.name, ln)
                            self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
                            self._sink.flush()

                    # LogMode.MEMORY: stub — emit nothing (future work).

                self._follow_clock.sleep(_FOLLOW_POLL_INTERVAL)
        except BrokenPipeError:
            return 0

        return 130
