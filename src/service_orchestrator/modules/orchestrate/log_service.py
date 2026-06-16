"""Log read service — implements the ``logs`` action (backlog + follow).

Reads persisted log files written by ``logwriter.py`` and emits NDJSON to
an injected output sink.  Pure helpers are free functions so they unit-test
without I/O.

Line format on disk: ``<RFC3339-Z ts>\\t<raw msg>\\n`` (TAB-separated).
Rotation: ``<svc>.log`` is newest; ``<svc>.log.1`` … ``<svc>.log.<N>`` are
older segments, higher number = older.

Follow mode:
  When ``query.follow`` is True the backlog is emitted first (honoring
  ``query.tail``), then the loop polls each service's active log file for
  new lines at a 0.25-second interval.  The loop exits when
  ``follow_clock.interrupted()`` returns True and the caller returns 130.
  A ``BrokenPipeError`` from the sink (consumer closed the pipe) exits the
  loop with return code 0 and suppresses the traceback.

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

from service_manifest.modules.manifest.model import LogMode
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.log_query import LogQuery
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
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


def build_event(ts: str | None, svc: str, msg: str) -> dict[str, str]:
    """Build an NDJSON event dict.

    The ``ts`` key is omitted when *ts* is ``None`` (no per-line timestamp).
    """
    event: dict[str, str] = {}
    if ts is not None:
        event["ts"] = ts
    event["svc"] = svc
    event["msg"] = msg
    return event


def merge_sorted(
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
            all_events.append(build_event(ts, svc, msg))

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

    def logs(self, ctx: EnvContext, query: LogQuery) -> int:
        """Emit NDJSON for the requested services.

        Non-follow (backlog) path:
          1. Determine in-scope services (filtered by ``query.services`` if set,
             else all manifest services in manifest order).
          2. For each service: read based on its LogMode (FILE reads segment
             files; PANE reads via capture-pane; MEMORY emits a STDERR note).
          3. Apply ``since``/``until`` filter (events without ``ts`` are kept).
          4. Merge across services, sort by ``ts``.
          5. Apply ``query.tail`` to the filtered set.
          6. Emit each event as a compact NDJSON line to ``self._sink``.
          Returns 0.

        Follow path (``query.follow`` is True):
          Steps 1-6 above (backlog, filtered, tail-trimmed), then live polling.
          FILE-mode services are polled via byte-offset reads.
          PANE-mode services are re-captured each tick; only lines not yet
          emitted (new tail lines since last tick) are output — best-effort
          approximation (no guarantee of no duplicates on rapid rotation).
          Returns 130 when interrupted via SIGINT; returns 0 on BrokenPipeError
          (consumer closed the pipe).
        """
        requested = set(query.services)
        if requested:
            in_scope = [s for s in ctx.manifest.services if s.name in requested]
        else:
            in_scope = list(ctx.manifest.services)

        # --- backlog ---
        # file_seeds[svc] records the byte boundary the active-file backlog read
        # consumed, so the follow loop can resume from exactly there (no gap, no
        # duplicate across the backlog→follow handoff).
        streams: list[tuple[str, list[tuple[str | None, str]]]] = []
        file_seeds: dict[str, int] = {}
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
                        raw_lines, file_seeds[svc.name] = self._log_repo.read_new_lines(
                            active, 0
                        )
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
                if query.tail is not None:
                    lines = lines[-query.tail:]
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

        # Merge across services (build events + stable time-sort) → apply
        # since/until filter (order-preserving) → apply tail. Order of operations
        # ensures tail is the last N of the *filtered* set.
        #
        # The backlog→follow handoff is gap-free: the follow loop seeds each FILE
        # service's offset from file_seeds — the exact byte boundary this backlog
        # read consumed — so a line appended between backlog and follow is picked
        # up by the first poll tick rather than dropped or double-emitted.
        #
        # v1 approximation: when both FILE and PANE services are in scope, the
        # global tail trims the mixed-mode merged set; PANE events (no ts) sort
        # before FILE events and may consume tail slots, making the effective
        # backlog for FILE-mode services smaller than requested.
        all_events = merge_sorted(streams)
        all_events = apply_time_filter(all_events, query.since, query.until)
        file_events = apply_tail(all_events, query.tail)

        try:
            for event in file_events:
                self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._sink.flush()
        except BrokenPipeError:
            return 0

        if not query.follow:
            return 0

        # --- live follow ---
        self._follow_clock.install()

        # FILE-mode: seed per-service byte offsets from the boundary the backlog
        # read consumed (file_seeds). A service whose active file did not exist
        # at backlog time has no entry and starts from 0, so its first appended
        # line is emitted in full.
        offsets: dict[str, int] = {}
        for svc in in_scope:
            if svc.log == LogMode.FILE:
                offsets[svc.name] = file_seeds.get(svc.name, 0)

        # PANE-mode follow: track the number of lines already emitted per service
        # so we can emit only new lines on each tick (best-effort; if the pane
        # scrolls and lines disappear, we may miss or re-emit some lines).
        pane_emitted_counts: dict[str, int] = {}
        for svc in in_scope:
            if svc.log == LogMode.PANE:
                pane_emitted_counts[svc.name] = 0

        try:
            while not self._follow_clock.interrupted():
                for svc in in_scope:
                    if svc.log == LogMode.FILE:
                        active = self._log_repo.log_path(ctx.worktree_dir, svc.name)
                        current_size = self._log_repo.file_size(active)

                        # Rotation/truncation: file shrank → reset to start of new segment.
                        if current_size < offsets[svc.name]:
                            offsets[svc.name] = 0

                        new_lines, new_offset = self._log_repo.read_new_lines(
                            active, offsets[svc.name]
                        )
                        offsets[svc.name] = new_offset

                        for raw in new_lines:
                            if not raw:
                                continue
                            ts, msg = parse_line(raw)
                            # Apply since/until filter to each live line (same
                            # semantics as the backlog path; pane events have no
                            # ts and are always kept).
                            if ts is not None:
                                if query.since and ts < query.since:
                                    continue
                                if query.until and ts > query.until:
                                    continue
                            event = build_event(ts, svc.name, msg)
                            self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
                            self._sink.flush()

                    elif svc.log == LogMode.PANE:
                        # Best-effort pane follow: re-capture the pane and emit
                        # only lines beyond the previously emitted count.
                        target = f"{svc.target.window}.{svc.target.pane}"
                        try:
                            captured = self._tmux.capture_pane(ctx.session, target)
                        except Exception:
                            # Session gone or pane missing — skip silently this tick.
                            continue
                        all_lines = [ln for ln in captured.splitlines() if ln]
                        prev_count = pane_emitted_counts[svc.name]
                        new_lines_pane = all_lines[prev_count:]
                        pane_emitted_counts[svc.name] = len(all_lines)
                        for ln in new_lines_pane:
                            event = build_event(None, svc.name, ln)
                            self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
                            self._sink.flush()

                    # LogMode.MEMORY: stub — emit nothing (future work).

                self._follow_clock.sleep(_FOLLOW_POLL_INTERVAL)
        except BrokenPipeError:
            return 0

        return 130
