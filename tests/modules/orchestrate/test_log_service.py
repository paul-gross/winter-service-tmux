"""Tests for LogService — backlog read, NDJSON emit, merge-sort, tail, filter, follow.

All tests use FakeLogRepository and FakeFollowClock so no real filesystem I/O,
real sleep, or real SIGINT handling occurs.
The output sink is an in-memory StringIO so no capsys is needed.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from service_manifest.modules.manifest.model import LogMode, Service, ServiceManifest, Target
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.log_query import LogQuery
from service_orchestrator.modules.orchestrate.log_service import (
    LogService,
    apply_tail,
    apply_time_filter,
    build_event,
    merge_sorted,
    parse_line,
)
from tests.conftest import FakeFollowClock, FakeLogRepository, FakeTmuxRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_WORKTREE = _WORKSPACE / "alpha"


def _make_manifest(*service_names: str) -> ServiceManifest:
    services = tuple(
        Service(name=n, target=Target(window=0, pane=i), command="cmd") for i, n in enumerate(service_names)
    )
    return ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=services,
        status_urls=(),
    )


def _make_ctx(manifest: ServiceManifest) -> EnvContext:
    return EnvContext(
        env="alpha",
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKTREE,
        manifest=manifest,
        env_vars=None,
        env_file_path=None,
    )


def _make_query(
    services: tuple[str, ...] = (),
    tail: int | None = None,
    follow: bool = False,
) -> LogQuery:
    return LogQuery(
        services=services,
        follow=follow,
        tail=tail,
        since="",
        until="",
        timestamps=False,
    )


def _make_svc(
    log_repo: FakeLogRepository,
    sink: io.StringIO,
    clock: FakeFollowClock | None = None,
    tmux: FakeTmuxRepository | None = None,
) -> LogService:
    """Construct a LogService with a fake clock (defaults to never-follow clock)."""
    return LogService(
        log_repo=log_repo,
        follow_clock=clock or FakeFollowClock(),
        tmux=tmux or FakeTmuxRepository(),
        sink=sink,
    )


def _run(service: LogService, ctx: EnvContext, query: LogQuery) -> tuple[list[dict], int]:
    """Run LogService.logs and return ``(parsed_events, rc)``."""
    sink = io.StringIO()
    service._sink = sink
    rc = service.logs(ctx, query)
    sink.seek(0)
    return [json.loads(line) for line in sink if line.strip()], rc


# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------


def test_parse_line_with_tab() -> None:
    ts, msg = parse_line("2026-06-15T10:00:01.000000Z\thello world")
    assert ts == "2026-06-15T10:00:01.000000Z"
    assert msg == "hello world"


def test_parse_line_no_tab() -> None:
    ts, msg = parse_line("raw line with no timestamp")
    assert ts is None
    assert msg == "raw line with no timestamp"


def test_build_event_with_ts() -> None:
    event = build_event("2026-06-15T10:00:01Z", "alpha", "api", "started")
    assert event == {"ts": "2026-06-15T10:00:01Z", "env": "alpha", "svc": "api", "msg": "started"}
    # Field ordering: ts, env, svc, msg
    assert list(event.keys()) == ["ts", "env", "svc", "msg"]


def test_build_event_without_ts() -> None:
    event = build_event(None, "alpha", "api", "started")
    assert event == {"env": "alpha", "svc": "api", "msg": "started"}
    assert "ts" not in event
    # Field ordering without ts: env, svc, msg
    assert list(event.keys()) == ["env", "svc", "msg"]


def test_merge_sorted_single_service() -> None:
    streams: list[tuple[str, list[tuple[str | None, str]]]] = [
        (
            "api",
            [
                ("2026-06-15T10:00:01Z", "first"),
                ("2026-06-15T10:00:03Z", "third"),
            ],
        )
    ]
    events = merge_sorted("alpha", streams)
    assert len(events) == 2
    assert events[0]["msg"] == "first"
    assert events[1]["msg"] == "third"
    assert events[0]["env"] == "alpha"


def test_merge_sorted_multi_service_by_ts() -> None:
    streams: list[tuple[str, list[tuple[str | None, str]]]] = [
        (
            "api",
            [
                ("2026-06-15T10:00:01Z", "api-first"),
                ("2026-06-15T10:00:03Z", "api-third"),
            ],
        ),
        (
            "worker",
            [
                ("2026-06-15T10:00:02Z", "worker-second"),
            ],
        ),
    ]
    events = merge_sorted("alpha", streams)
    assert [e["msg"] for e in events] == ["api-first", "worker-second", "api-third"]
    assert [e["svc"] for e in events] == ["api", "worker", "api"]
    assert all(e["env"] == "alpha" for e in events)


def test_merge_sorted_no_ts_sorts_first() -> None:
    """Events without ts sort before those with ts (empty string < any ts)."""
    streams = [
        (
            "api",
            [
                (None, "no-ts-line"),
                ("2026-06-15T10:00:01Z", "has-ts"),
            ],
        )
    ]
    events = merge_sorted("alpha", streams)
    assert events[0]["msg"] == "no-ts-line"
    assert "ts" not in events[0]
    assert events[1]["msg"] == "has-ts"
    assert events[0]["env"] == "alpha"


def test_apply_tail_none_returns_all() -> None:
    events = [{"svc": "a", "msg": str(i)} for i in range(10)]
    assert apply_tail(events, None) == events


def test_apply_tail_keeps_last_n() -> None:
    events = [{"svc": "a", "msg": str(i)} for i in range(10)]
    result = apply_tail(events, 3)
    assert len(result) == 3
    assert [e["msg"] for e in result] == ["7", "8", "9"]


def test_apply_tail_larger_than_list_returns_all() -> None:
    events = [{"svc": "a", "msg": str(i)} for i in range(3)]
    assert apply_tail(events, 100) == events


# ---------------------------------------------------------------------------
# LogService backlog tests (via FakeLogRepository)
# ---------------------------------------------------------------------------


def test_single_service_backlog_emits_ndjson() -> None:
    fake_repo = FakeLogRepository(
        segments={"api": ["2026-06-15T10:00:01.000000Z\tlistening on :8080\n2026-06-15T10:00:02.000000Z\tready\n"]}
    )
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 2
    assert events[0]["svc"] == "api"
    assert events[0]["ts"] == "2026-06-15T10:00:01.000000Z"
    assert events[0]["msg"] == "listening on :8080"
    assert events[0]["env"] == "alpha"
    # Field ordering: ts, env, svc, msg
    assert list(events[0].keys()) == ["ts", "env", "svc", "msg"]
    assert events[1]["msg"] == "ready"
    assert events[1]["env"] == "alpha"


def test_multi_service_merge_sorted() -> None:
    fake_repo = FakeLogRepository(
        segments={
            "api": ["2026-06-15T10:00:01Z\tapi-first\n2026-06-15T10:00:03Z\tapi-third\n"],
            "worker": ["2026-06-15T10:00:02Z\tworker-second\n"],
        }
    )
    manifest = _make_manifest("api", "worker")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert [e["msg"] for e in events] == ["api-first", "worker-second", "api-third"]
    assert [e["svc"] for e in events] == ["api", "worker", "api"]


def test_tail_keeps_last_n_events() -> None:
    lines = "".join(f"2026-06-15T10:00:0{i}Z\tline-{i}\n" for i in range(5))
    fake_repo = FakeLogRepository(segments={"api": [lines]})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query(tail=3))

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 3
    assert events[0]["msg"] == "line-2"
    assert events[2]["msg"] == "line-4"


def test_services_filter_limits_output() -> None:
    fake_repo = FakeLogRepository(
        segments={
            "api": ["2026-06-15T10:00:01Z\tapi-msg\n"],
            "worker": ["2026-06-15T10:00:02Z\tworker-msg\n"],
        }
    )
    manifest = _make_manifest("api", "worker")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query(services=("api",)))

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 1
    assert events[0]["svc"] == "api"


def test_no_tab_line_emits_event_without_ts() -> None:
    fake_repo = FakeLogRepository(segments={"api": ["line with no timestamp at all\n"]})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 1
    assert events[0]["msg"] == "line with no timestamp at all"
    assert "ts" not in events[0]
    assert events[0]["svc"] == "api"


def test_empty_log_file_emits_no_events() -> None:
    fake_repo = FakeLogRepository(segments={"api": [""]})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert events == []


def test_absent_log_file_emits_no_events() -> None:
    """Service with no segments in repo emits nothing (no log files)."""
    fake_repo = FakeLogRepository(segments={})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert events == []


def test_multi_segment_oldest_first() -> None:
    """When multiple segments exist, older segments appear before newer ones."""
    # Two segments: oldest (segment 1) has "old-line", newest (active) has "new-line".
    fake_repo = FakeLogRepository(
        segments={
            "api": [
                "2026-06-15T09:00:00Z\told-line\n",  # oldest segment
                "2026-06-15T10:00:00Z\tnew-line\n",  # newest (active) segment
            ]
        }
    )
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 2
    assert events[0]["msg"] == "old-line"
    assert events[1]["msg"] == "new-line"


# ---------------------------------------------------------------------------
# LogService follow tests
# ---------------------------------------------------------------------------


def test_follow_emits_backlog_first_then_exits_130_on_interrupt() -> None:
    """Backlog is emitted before live poll; interrupt exits with rc=130."""
    fake_repo = FakeLogRepository(segments={"api": ["2026-06-15T10:00:01Z\tbacklog-line\n"]})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    # Seed an empty active file (no new lines during follow).
    active = fake_repo.log_path(_WORKTREE, "api")
    fake_repo.seed_live_content(active, "")

    # Clock: first tick = not interrupted, second tick = interrupted.
    clock = FakeFollowClock(tick_results=[False, True])
    svc = _make_svc(fake_repo, sink, clock=clock)

    rc = svc.logs(ctx, _make_query(follow=True))

    assert rc == 130
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    # Backlog line must appear.
    assert any(e["msg"] == "backlog-line" for e in events)


def test_follow_tail_limits_backlog_before_live() -> None:
    """query.tail is applied to the backlog before live polling starts."""
    lines = "".join(f"2026-06-15T10:00:0{i}Z\tline-{i}\n" for i in range(5))
    fake_repo = FakeLogRepository(segments={"api": [lines]})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE, "api")
    fake_repo.seed_live_content(active, "")

    # Immediately interrupted — just backlog + exit.
    clock = FakeFollowClock(tick_results=[True])
    svc = _make_svc(fake_repo, sink, clock=clock)

    rc = svc.logs(ctx, _make_query(follow=True, tail=2))

    assert rc == 130
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 2
    assert events[0]["msg"] == "line-3"
    assert events[1]["msg"] == "line-4"


def test_follow_live_lines_emitted_between_ticks() -> None:
    """Lines appended to the active file between ticks are emitted live."""
    fake_repo = FakeLogRepository(segments={"api": []})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE, "api")
    # No content at follow start (backlog offset = 0).
    fake_repo.seed_live_content(active, "")

    # We need the live content to grow between ticks.  The FakeFollowClock
    # supports a side-effect callable for this.  We patch seed_live_content
    # to grow after the first tick.
    appended: list[bool] = []

    tick_count: list[int] = [0]

    class GrowingClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2 and not appended:
                # Append a new line to the fake active file.
                fake_repo.seed_live_content(active, "2026-06-15T10:00:05Z\tlive-line\n")
                appended.append(True)
            return tick_count[0] > 3

    clock = GrowingClock()
    svc = _make_svc(fake_repo, sink, clock=clock)

    rc = svc.logs(ctx, _make_query(follow=True))

    assert rc == 130
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert any(e["msg"] == "live-line" for e in events)


def test_follow_broken_pipe_returns_0() -> None:
    """A BrokenPipeError from the sink during live follow returns rc=0."""
    fake_repo = FakeLogRepository(segments={"api": []})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)

    active = fake_repo.log_path(_WORKTREE, "api")
    # Start with empty file so offset=0.
    fake_repo.seed_live_content(active, "")

    live_content_appended: list[bool] = []

    class BrokenSink(io.StringIO):
        def write(self, s: str) -> int:
            raise BrokenPipeError("consumer closed")

    broken = BrokenSink()
    tick_count: list[int] = [0]

    class GrowThenBrokenClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 1 and not live_content_appended:
                # Grow the file so the next tick finds new lines.
                fake_repo.seed_live_content(active, "2026-06-15T10:00:01Z\tlive\n")
                live_content_appended.append(True)
            # Never naturally interrupt — BrokenPipeError must exit the loop.
            return False

    clock = GrowThenBrokenClock()
    svc = LogService(log_repo=fake_repo, follow_clock=clock, tmux=FakeTmuxRepository(), sink=broken)

    rc = svc.logs(ctx, _make_query(follow=True))

    assert rc == 0


def test_follow_rotation_resets_offset() -> None:
    """If the active file shrinks (rotation/truncation), offset resets to 0.

    Scenario: at follow start the active file has 50+ bytes (all consumed,
    offset = 50).  On tick 2 a rotation occurs: the active file is replaced
    with a fresh short file (< 50 bytes).  The loop must detect the shrink,
    reset offset to 0, and emit the new line.
    """
    fake_repo = FakeLogRepository(segments={"api": []})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE, "api")
    # Initial content is long enough that offset will be > len(fresh_content).
    initial_content = "2026-06-15T10:00:01Z\told-line-with-lots-of-extra-content\n"
    fake_repo.seed_live_content(active, initial_content)

    # fresh content is deliberately shorter than initial_content so that
    # file_size < offset triggers the rotation reset.
    fresh_content = "2026-06-15T10:00:10Z\tnew\n"
    assert len(fresh_content) < len(initial_content)  # sanity

    tick_count: list[int] = [0]

    class RotatingClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                # Simulate rotation: file is now shorter than the tracked offset.
                fake_repo.seed_live_content(active, fresh_content)
            return tick_count[0] > 4

    clock = RotatingClock()
    svc = _make_svc(fake_repo, sink, clock=clock)

    rc = svc.logs(ctx, _make_query(follow=True))

    assert rc == 130
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert any(e["msg"] == "new" for e in events)


def test_follow_seeds_from_backlog_boundary_not_file_end() -> None:
    """A line incomplete at backlog time is emitted by follow once completed.

    Regression for the backlog->follow handoff: the follow offset is seeded
    from the byte boundary the backlog read consumed (end of the last complete
    line), NOT the current end of the file. Seeding from the file end would
    skip past a trailing partial line, dropping it permanently even after it
    is completed. Here the active file holds one complete line plus a partial
    line at follow start; once the partial line gains its newline mid-follow it
    must be emitted exactly once.
    """
    complete = "2026-06-15T10:00:01Z\tcomplete-line\n"
    partial = "2026-06-15T10:00:02Z\tlate-line"  # no trailing newline yet
    fake_repo = FakeLogRepository(segments={"api": [complete + partial]})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE, "api")
    tick_count: list[int] = [0]

    class CompletingClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                # The partial line gains its newline (writer finished it).
                fake_repo.seed_live_content(active, complete + partial + "\n")
            return tick_count[0] > 3

    svc = _make_svc(fake_repo, sink, clock=CompletingClock())

    rc = svc.logs(ctx, _make_query(follow=True))

    assert rc == 130
    sink.seek(0)
    msgs = [json.loads(line)["msg"] for line in sink if line.strip()]
    # Backlog emits the complete line; follow emits the late line exactly once.
    assert msgs == ["complete-line", "late-line"]


def test_follow_install_is_called() -> None:
    """follow_clock.install() is called before the follow loop starts."""
    fake_repo = FakeLogRepository(segments={"api": []})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE, "api")
    fake_repo.seed_live_content(active, "")

    clock = FakeFollowClock(tick_results=[True])
    svc = _make_svc(fake_repo, sink, clock=clock)

    svc.logs(ctx, _make_query(follow=True))

    assert clock.install_called


def test_follow_sleep_called_per_tick() -> None:
    """follow_clock.sleep() is called once per non-interrupted tick."""
    fake_repo = FakeLogRepository(segments={"api": []})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE, "api")
    fake_repo.seed_live_content(active, "")

    # Two non-interrupted ticks then interrupt.
    clock = FakeFollowClock(tick_results=[False, False, True])
    svc = _make_svc(fake_repo, sink, clock=clock)

    svc.logs(ctx, _make_query(follow=True))

    assert len(clock.sleep_calls) == 2


# ---------------------------------------------------------------------------
# RealFollowClock unit test
# ---------------------------------------------------------------------------


def test_real_follow_clock_not_interrupted_initially() -> None:
    """RealFollowClock reports not-interrupted before any signal is received."""
    from service_orchestrator.modules.orchestrate.internal.real_follow_clock import (
        RealFollowClock,
    )

    clock = RealFollowClock()
    assert clock.interrupted() is False


# ---------------------------------------------------------------------------
# LogMode.PANE — backlog via capture_pane, no ts
# ---------------------------------------------------------------------------


def _make_pane_manifest(name: str, window: int = 0, pane: int = 0) -> ServiceManifest:
    """Build a single-service manifest with log=LogMode.PANE."""
    svc = Service(name=name, target=Target(window=window, pane=pane), command="cmd", log=LogMode.PANE)
    return ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(svc,),
        status_urls=(),
    )


def test_pane_mode_backlog_emits_ndjson_without_ts() -> None:
    """PANE-mode service: capture_pane content is emitted as NDJSON with no 'ts' key."""
    tmux = FakeTmuxRepository(capture_text={"0.0": "line one\nline two\n"})
    tmux.seed_session("mp-alpha", {"0.0": 10})
    fake_repo = FakeLogRepository()
    manifest = _make_pane_manifest("shell", window=0, pane=0)
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, tmux=tmux)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 2
    assert events[0]["svc"] == "shell"
    assert events[0]["msg"] == "line one"
    assert "ts" not in events[0]
    assert events[0]["env"] == "alpha"
    # Field ordering without ts: env, svc, msg
    assert list(events[0].keys()) == ["env", "svc", "msg"]
    assert events[1]["msg"] == "line two"
    assert "ts" not in events[1]
    assert events[1]["env"] == "alpha"


def test_pane_mode_tail_limits_output() -> None:
    """PANE-mode: tail limits to the last N captured lines."""
    lines = "\n".join(f"pane-line-{i}" for i in range(10)) + "\n"
    tmux = FakeTmuxRepository(capture_text={"0.0": lines})
    tmux.seed_session("mp-alpha", {"0.0": 10})
    fake_repo = FakeLogRepository()
    manifest = _make_pane_manifest("shell")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, tmux=tmux)

    rc = svc.logs(ctx, _make_query(tail=3))

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 3
    assert events[0]["msg"] == "pane-line-7"
    assert events[2]["msg"] == "pane-line-9"


def test_pane_mode_services_filter() -> None:
    """PANE-mode: services filter correctly limits to the requested service."""
    tmux = FakeTmuxRepository(capture_text={"0.0": "shell-line\n", "0.1": "other-line\n"})
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    fake_repo = FakeLogRepository()
    svc_shell = Service(name="shell", target=Target(window=0, pane=0), command="", log=LogMode.PANE)
    svc_other = Service(name="other", target=Target(window=0, pane=1), command="cmd", log=LogMode.PANE)
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(svc_shell, svc_other),
        status_urls=(),
    )
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    service = _make_svc(fake_repo, sink, tmux=tmux)

    rc = service.logs(ctx, _make_query(services=("shell",)))

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert len(events) == 1
    assert events[0]["svc"] == "shell"


def test_pane_mode_empty_capture_emits_nothing() -> None:
    """PANE-mode: empty capture_pane content produces no events."""
    tmux = FakeTmuxRepository(capture_text={"0.0": ""})
    tmux.seed_session("mp-alpha", {"0.0": 10})
    fake_repo = FakeLogRepository()
    manifest = _make_pane_manifest("shell")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, tmux=tmux)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert events == []


# ---------------------------------------------------------------------------
# LogMode.MEMORY — stub: emits nothing
# ---------------------------------------------------------------------------


def test_memory_mode_emits_nothing() -> None:
    """MEMORY-mode service: no output (stub, future work)."""
    svc_mem = Service(name="svc", target=Target(window=0, pane=0), command="cmd", log=LogMode.MEMORY)
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(svc_mem,),
        status_urls=(),
    )
    fake_repo = FakeLogRepository()
    tmux = FakeTmuxRepository()
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, tmux=tmux)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    assert events == []


def test_memory_mode_emits_one_line_stderr_diagnostic(capsys: pytest.CaptureFixture[str]) -> None:
    """MEMORY-mode service emits exactly one diagnostic line to stderr explaining
    why there is no output, so callers see a clear message instead of silence.
    """
    svc_mem = Service(name="worker", target=Target(window=0, pane=0), command="cmd", log=LogMode.MEMORY)
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(svc_mem,),
        status_urls=(),
    )
    fake_repo = FakeLogRepository()
    tmux = FakeTmuxRepository()
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, tmux=tmux)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    captured = capsys.readouterr()
    # Exactly one stderr line mentioning the service name and "memory"
    stderr_lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(stderr_lines) == 1
    assert "worker" in stderr_lines[0]
    assert "memory" in stderr_lines[0].lower()


# ---------------------------------------------------------------------------
# Mixed FILE + PANE in one logs call
# ---------------------------------------------------------------------------


def test_mixed_file_and_pane_both_contribute() -> None:
    """A call covering one FILE-mode and one PANE-mode service emits events from both."""
    # FILE service (api): has a log file
    svc_file = Service(name="api", target=Target(window=0, pane=0), command="cmd", log=LogMode.FILE)
    # PANE service (shell): read via capture-pane
    svc_pane = Service(name="shell", target=Target(window=0, pane=1), command="", log=LogMode.PANE)
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(svc_file, svc_pane),
        status_urls=(),
    )
    fake_repo = FakeLogRepository(segments={"api": ["2026-06-16T10:00:01Z\tapi-msg\n"]})
    tmux = FakeTmuxRepository(capture_text={"0.1": "shell-line\n"})
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, tmux=tmux)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    svcs = {e["svc"] for e in events}
    assert "api" in svcs
    assert "shell" in svcs
    api_events = [e for e in events if e["svc"] == "api"]
    shell_events = [e for e in events if e["svc"] == "shell"]
    assert api_events[0]["msg"] == "api-msg"
    assert "ts" in api_events[0]
    assert shell_events[0]["msg"] == "shell-line"
    assert "ts" not in shell_events[0]


# ---------------------------------------------------------------------------
# PANE follow — best-effort new-lines across ticks
# ---------------------------------------------------------------------------


def test_pane_follow_emits_new_lines_across_ticks() -> None:
    """PANE follow: new lines in capture_pane on each tick are emitted once."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    # Start with 2 lines; tick 2 adds a third.
    tmux.capture_text["0.0"] = "tick1-line1\ntick1-line2\n"

    manifest = _make_pane_manifest("shell")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()

    tick_count: list[int] = [0]

    class GrowingCaptureClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                tmux.capture_text["0.0"] = "tick1-line1\ntick1-line2\ntick2-new\n"
            return tick_count[0] > 3

    clock = GrowingCaptureClock()
    fake_repo = FakeLogRepository()
    svc = _make_svc(fake_repo, sink, clock=clock, tmux=tmux)

    rc = svc.logs(ctx, _make_query(follow=True))

    assert rc == 130
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    msgs = [e["msg"] for e in events]
    # The backlog emitted tick1-line1 and tick1-line2 first.
    # tick2-new should appear (emitted during follow loop).
    assert "tick2-new" in msgs
    # No ts on any pane event.
    for e in events:
        assert "ts" not in e


# ---------------------------------------------------------------------------
# apply_time_filter — pure unit tests
# ---------------------------------------------------------------------------


def test_time_filter_no_bounds_returns_all() -> None:
    """Empty since and until: all events returned unchanged."""
    events = [
        {"ts": "2026-06-15T10:00:01Z", "svc": "a", "msg": "m1"},
        {"ts": "2026-06-15T10:00:02Z", "svc": "a", "msg": "m2"},
    ]
    assert apply_time_filter(events, "", "") == events


def test_time_filter_since_inclusive_boundary_kept() -> None:
    """Event exactly at since boundary is kept (inclusive)."""
    events = [
        {"ts": "2026-06-15T10:00:01Z", "svc": "a", "msg": "before"},
        {"ts": "2026-06-15T10:00:02Z", "svc": "a", "msg": "at-since"},
        {"ts": "2026-06-15T10:00:03Z", "svc": "a", "msg": "after"},
    ]
    result = apply_time_filter(events, since="2026-06-15T10:00:02Z", until="")
    msgs = [e["msg"] for e in result]
    assert "before" not in msgs
    assert "at-since" in msgs
    assert "after" in msgs


def test_time_filter_until_inclusive_boundary_kept() -> None:
    """Event exactly at until boundary is kept (inclusive)."""
    events = [
        {"ts": "2026-06-15T10:00:01Z", "svc": "a", "msg": "before"},
        {"ts": "2026-06-15T10:00:02Z", "svc": "a", "msg": "at-until"},
        {"ts": "2026-06-15T10:00:03Z", "svc": "a", "msg": "after"},
    ]
    result = apply_time_filter(events, since="", until="2026-06-15T10:00:02Z")
    msgs = [e["msg"] for e in result]
    assert "before" in msgs
    assert "at-until" in msgs
    assert "after" not in msgs


def test_time_filter_outside_both_bounds_dropped() -> None:
    """Events outside both since and until bounds are dropped."""
    events = [
        {"ts": "2026-06-15T09:00:00Z", "svc": "a", "msg": "too-early"},
        {"ts": "2026-06-15T10:00:01Z", "svc": "a", "msg": "in-range"},
        {"ts": "2026-06-15T11:00:00Z", "svc": "a", "msg": "too-late"},
    ]
    result = apply_time_filter(
        events,
        since="2026-06-15T10:00:00Z",
        until="2026-06-15T10:30:00Z",
    )
    msgs = [e["msg"] for e in result]
    assert "too-early" not in msgs
    assert "in-range" in msgs
    assert "too-late" not in msgs


def test_time_filter_no_ts_always_kept() -> None:
    """Events without ts (pane-mode) are always kept regardless of bounds."""
    events = [
        {"svc": "shell", "msg": "pane-line"},
        {"ts": "2026-06-15T09:00:00Z", "svc": "api", "msg": "before"},
    ]
    result = apply_time_filter(events, since="2026-06-15T10:00:00Z", until="")
    msgs = [e["msg"] for e in result]
    assert "pane-line" in msgs
    assert "before" not in msgs


def test_time_filter_since_only_filters_correctly() -> None:
    """since with no until only drops events before the bound."""
    events = [
        {"ts": "2026-06-15T10:00:00Z", "svc": "a", "msg": "at-boundary"},
        {"ts": "2026-06-15T10:00:01Z", "svc": "a", "msg": "after"},
        {"ts": "2026-06-15T09:59:59Z", "svc": "a", "msg": "before"},
    ]
    result = apply_time_filter(events, since="2026-06-15T10:00:00Z", until="")
    msgs = [e["msg"] for e in result]
    assert "before" not in msgs
    assert "at-boundary" in msgs
    assert "after" in msgs


def test_time_filter_until_only_filters_correctly() -> None:
    """until with no since only drops events after the bound."""
    events = [
        {"ts": "2026-06-15T10:00:00Z", "svc": "a", "msg": "at-boundary"},
        {"ts": "2026-06-15T09:59:59Z", "svc": "a", "msg": "before"},
        {"ts": "2026-06-15T10:00:01Z", "svc": "a", "msg": "after"},
    ]
    result = apply_time_filter(events, since="", until="2026-06-15T10:00:00Z")
    msgs = [e["msg"] for e in result]
    assert "before" in msgs
    assert "at-boundary" in msgs
    assert "after" not in msgs


# ---------------------------------------------------------------------------
# since/until integration through LogService.logs
# ---------------------------------------------------------------------------


def test_logs_since_filters_events_before_bound() -> None:
    """LogService.logs respects query.since — events before the bound are dropped."""
    fake_repo = FakeLogRepository(
        segments={"api": ["2026-06-15T09:00:00Z\tearly\n2026-06-15T10:00:00Z\tat-since\n2026-06-15T10:00:01Z\tafter\n"]}
    )
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    query = LogQuery(services=(), follow=False, tail=None, since="2026-06-15T10:00:00Z", until="", timestamps=False)
    rc = svc.logs(ctx, query)

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    msgs = [e["msg"] for e in events]
    assert "early" not in msgs
    assert "at-since" in msgs
    assert "after" in msgs


def test_logs_until_filters_events_after_bound() -> None:
    """LogService.logs respects query.until — events after the bound are dropped."""
    fake_repo = FakeLogRepository(
        segments={
            "api": ["2026-06-15T10:00:00Z\tbefore\n2026-06-15T10:00:01Z\tat-until\n2026-06-15T10:00:02Z\tafter\n"]
        }
    )
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    query = LogQuery(services=(), follow=False, tail=None, since="", until="2026-06-15T10:00:01Z", timestamps=False)
    rc = svc.logs(ctx, query)

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    msgs = [e["msg"] for e in events]
    assert "before" in msgs
    assert "at-until" in msgs
    assert "after" not in msgs


def test_logs_tail_applies_after_time_filter() -> None:
    """tail is applied to the filtered set, not the raw backlog."""
    fake_repo = FakeLogRepository(
        segments={
            "api": [
                "2026-06-15T09:00:00Z\texcluded\n"
                "2026-06-15T10:00:00Z\tincluded-1\n"
                "2026-06-15T10:00:01Z\tincluded-2\n"
                "2026-06-15T10:00:02Z\tincluded-3\n"
            ]
        }
    )
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    # Filtered set has 3 events; tail=2 should keep the last 2.
    query = LogQuery(services=(), follow=False, tail=2, since="2026-06-15T10:00:00Z", until="", timestamps=False)
    rc = svc.logs(ctx, query)

    assert rc == 0
    sink.seek(0)
    events = [json.loads(line) for line in sink if line.strip()]
    msgs = [e["msg"] for e in events]
    assert len(events) == 2
    assert "included-2" in msgs
    assert "included-3" in msgs
    assert "excluded" not in msgs
    assert "included-1" not in msgs


# ---------------------------------------------------------------------------
# Door-boundary NDJSON env field — serialized wire lines for file and pane mode
# ---------------------------------------------------------------------------


def test_ndjson_serialized_line_contains_env_file_mode() -> None:
    """File-mode: each serialized NDJSON line on the wire contains the 'env' field."""
    fake_repo = FakeLogRepository(segments={"api": ["2026-06-15T10:00:00Z\thello\n"]})
    manifest = _make_manifest("api")
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    raw_lines = [line for line in sink if line.strip()]
    assert len(raw_lines) == 1
    # Parse the raw serialized line to assert env field is present
    parsed = json.loads(raw_lines[0])
    assert "env" in parsed
    assert parsed["env"] == "alpha"


def test_ndjson_serialized_line_contains_env_pane_mode() -> None:
    """Pane-mode: each serialized NDJSON line on the wire contains the 'env' field (no 'ts')."""
    from service_manifest.modules.manifest.model import LogMode

    tmux = FakeTmuxRepository(capture_text={"0.0": "pane-output\n"})
    tmux.seed_session("mp-alpha", {"0.0": 10})
    fake_repo = FakeLogRepository()
    svc_pane = Service(
        name="shell",
        target=Target(window=0, pane=0),
        command="",
        log=LogMode.PANE,
    )
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(svc_pane,),
        status_urls=(),
    )
    ctx = _make_ctx(manifest)
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, tmux=tmux)

    rc = svc.logs(ctx, _make_query())

    assert rc == 0
    sink.seek(0)
    raw_lines = [line for line in sink if line.strip()]
    assert len(raw_lines) == 1
    # Parse the raw serialized line to assert env field is present (no ts)
    parsed = json.loads(raw_lines[0])
    assert "env" in parsed
    assert parsed["env"] == "alpha"
    assert "ts" not in parsed
