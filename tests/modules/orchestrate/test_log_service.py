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


# ---------------------------------------------------------------------------
# follow_streams tests
# ---------------------------------------------------------------------------

_WORKSPACE_BETA = Path("/fake/workspace")
_WORKTREE_ALPHA = _WORKSPACE_BETA / "alpha"
_WORKTREE_BETA = _WORKSPACE_BETA / "beta"


def _make_dual_repo(alpha_repo: FakeLogRepository, beta_repo: FakeLogRepository) -> FakeLogRepository:
    """Return a FakeLogRepository that routes alpha/beta calls to the right underlying repo.

    Routing key: ``"alpha" in str(path/worktree_dir)`` → alpha_repo, else beta_repo.
    This consolidates the repeated CombinedFakeRepo/2/3 pattern across tests.
    """

    class DualFakeRepo(FakeLogRepository):
        def log_path(self, worktree_dir: Path, service: str) -> Path:
            return (alpha_repo if "alpha" in str(worktree_dir) else beta_repo).log_path(worktree_dir, service)

        def segment_files(self, worktree_dir: Path, service: str) -> list[Path]:
            return (alpha_repo if "alpha" in str(worktree_dir) else beta_repo).segment_files(worktree_dir, service)

        def read_lines(self, path: Path) -> list[str]:
            return (alpha_repo if "alpha" in str(path) else beta_repo).read_lines(path)

        def file_size(self, path: Path) -> int:
            return (alpha_repo if "alpha" in str(path) else beta_repo).file_size(path)

        def read_new_lines(self, path: Path, offset: int) -> tuple[list[str], int]:
            return (alpha_repo if "alpha" in str(path) else beta_repo).read_new_lines(path, offset)

    return DualFakeRepo()


def _make_ctx_env(env: str, manifest: ServiceManifest) -> EnvContext:
    """Build an EnvContext for *env* with a worktree at /fake/workspace/<env>."""
    worktree = Path("/fake/workspace") / env
    return EnvContext(
        env=env,
        workspace_root=Path("/fake/workspace"),
        worktree_dir=worktree,
        manifest=manifest,
        env_vars=None,
        env_file_path=None,
    )


def _run_follow(
    service: LogService,
    streams: list[tuple[EnvContext, LogQuery]],
) -> tuple[list[dict], int]:
    """Call follow_streams and return ``(parsed_events, rc)``."""
    sink = io.StringIO()
    service._sink = sink
    rc = service.follow_streams(streams)
    sink.seek(0)
    return [json.loads(line) for line in sink if line.strip()], rc


def test_follow_streams_single_stream_regression() -> None:
    """Single (ctx, query) pair: backlog emitted then live line; rc 130."""
    fake_repo = FakeLogRepository(segments={"backend": ["2026-06-15T10:00:01Z\tbacklog-line\n"]})
    manifest = _make_manifest("backend")
    ctx = _make_ctx_env("alpha", manifest)
    active = fake_repo.log_path(_WORKTREE_ALPHA, "backend")

    tick_count: list[int] = [0]

    class GrowingClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                fake_repo.seed_live_content(
                    active, "2026-06-15T10:00:01Z\tbacklog-line\n2026-06-15T10:00:05Z\tlive-line\n"
                )
            return tick_count[0] > 3

    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, clock=GrowingClock())

    query = _make_query()
    events, rc = _run_follow(svc, [(ctx, query)])

    assert rc == 130
    msgs = [e["msg"] for e in events]
    assert "backlog-line" in msgs
    assert "live-line" in msgs


def test_follow_streams_multi_service_single_env_interleaves() -> None:
    """Two FILE services in one env: merged time-sorted backlog; both grow live."""
    fake_repo = FakeLogRepository(
        segments={
            "api": ["2026-06-15T10:00:01Z\tapi-backlog\n"],
            "worker": ["2026-06-15T10:00:02Z\tworker-backlog\n"],
        }
    )
    manifest = _make_manifest("api", "worker")
    ctx = _make_ctx_env("alpha", manifest)

    active_api = fake_repo.log_path(_WORKTREE_ALPHA, "api")
    active_worker = fake_repo.log_path(_WORKTREE_ALPHA, "worker")

    tick_count: list[int] = [0]

    class GrowBothClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                fake_repo.seed_live_content(
                    active_api, "2026-06-15T10:00:01Z\tapi-backlog\n2026-06-15T10:00:10Z\tapi-live\n"
                )
                fake_repo.seed_live_content(
                    active_worker, "2026-06-15T10:00:02Z\tworker-backlog\n2026-06-15T10:00:11Z\tworker-live\n"
                )
            return tick_count[0] > 3

    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, clock=GrowBothClock())

    events, rc = _run_follow(svc, [(ctx, _make_query())])

    assert rc == 130
    msgs = [e["msg"] for e in events]
    # Backlog is time-sorted.
    assert msgs.index("api-backlog") < msgs.index("worker-backlog")
    # Both live lines appear.
    assert "api-live" in msgs
    assert "worker-live" in msgs
    # Correct svc tags.
    api_live = next(e for e in events if e["msg"] == "api-live")
    worker_live = next(e for e in events if e["msg"] == "worker-live")
    assert api_live["svc"] == "api"
    assert api_live["env"] == "alpha"
    assert worker_live["svc"] == "worker"
    assert worker_live["env"] == "alpha"


def test_follow_streams_cross_env_merged_backlog() -> None:
    """Two ctxs (alpha, beta) each with FILE backend: backlog merged sorted by ts; env field correct."""
    fake_repo = FakeLogRepository(
        segments={
            "backend": [
                "2026-06-15T10:00:02Z\talpha-backlog\n",
            ]
        }
    )
    # Beta uses its own repo instance so its segments are independent.
    fake_repo_beta = FakeLogRepository(
        segments={
            "backend": [
                "2026-06-15T10:00:01Z\tbeta-backlog\n",
            ]
        }
    )

    manifest_alpha = _make_manifest("backend")
    manifest_beta = _make_manifest("backend")

    ctx_alpha = _make_ctx_env("alpha", manifest_alpha)
    ctx_beta = _make_ctx_env("beta", manifest_beta)

    clock = FakeFollowClock(tick_results=[True])
    sink = io.StringIO()

    # Route alpha/beta calls to their respective repos via the shared dual-repo helper.
    combined = _make_dual_repo(fake_repo, fake_repo_beta)
    svc = LogService(log_repo=combined, follow_clock=clock, tmux=FakeTmuxRepository(), sink=sink)

    events, rc = _run_follow(svc, [(ctx_alpha, _make_query()), (ctx_beta, _make_query())])

    assert rc == 130
    # Beta has earlier ts → should sort first.
    msgs = [e["msg"] for e in events]
    assert msgs.index("beta-backlog") < msgs.index("alpha-backlog")
    # Each event carries the correct env.
    alpha_ev = next(e for e in events if e["msg"] == "alpha-backlog")
    beta_ev = next(e for e in events if e["msg"] == "beta-backlog")
    assert alpha_ev["env"] == "alpha"
    assert beta_ev["env"] == "beta"


def test_follow_streams_same_service_name_distinct_offsets() -> None:
    """Two envs both named 'backend': only beta's file grows → only beta's line emitted."""
    fake_repo_alpha = FakeLogRepository(segments={"backend": ["2026-06-15T10:00:01Z\talpha-backlog\n"]})
    fake_repo_beta = FakeLogRepository(segments={"backend": ["2026-06-15T10:00:02Z\tbeta-backlog\n"]})

    manifest_alpha = _make_manifest("backend")
    manifest_beta = _make_manifest("backend")

    ctx_alpha = _make_ctx_env("alpha", manifest_alpha)
    ctx_beta = _make_ctx_env("beta", manifest_beta)

    active_beta = fake_repo_beta.log_path(_WORKTREE_BETA, "backend")

    tick_count: list[int] = [0]

    class GrowBetaClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                # Only beta grows.
                fake_repo_beta.seed_live_content(
                    active_beta,
                    "2026-06-15T10:00:02Z\tbeta-backlog\n2026-06-15T10:00:10Z\tbeta-live\n",
                )
            return tick_count[0] > 3

    combined = _make_dual_repo(fake_repo_alpha, fake_repo_beta)
    sink = io.StringIO()
    svc = LogService(log_repo=combined, follow_clock=GrowBetaClock(), tmux=FakeTmuxRepository(), sink=sink)

    events, rc = _run_follow(svc, [(ctx_alpha, _make_query()), (ctx_beta, _make_query())])

    assert rc == 130
    live_events = [e for e in events if e.get("msg") == "beta-live"]
    assert len(live_events) == 1
    assert live_events[0]["env"] == "beta"
    # Alpha must emit no live events.
    alpha_live = [e for e in events if e.get("env") == "alpha" and e.get("msg") not in ("alpha-backlog",)]
    assert alpha_live == []


def test_follow_streams_global_tail_applies_to_merged_backlog() -> None:
    """Two streams, tail=2 → exactly last 2 of merged backlog (not 2 per stream)."""
    # Each list entry is one segment file; the two-line string is a single active
    # segment with two log lines (intentional adjacent-string-literal concat).
    fake_repo = FakeLogRepository(
        segments={"backend": ["2026-06-15T10:00:01Z\talpha-1\n2026-06-15T10:00:02Z\talpha-2\n"]}
    )
    fake_repo_beta = FakeLogRepository(
        segments={"backend": ["2026-06-15T10:00:03Z\tbeta-1\n2026-06-15T10:00:04Z\tbeta-2\n"]}
    )

    manifest_alpha = _make_manifest("backend")
    manifest_beta = _make_manifest("backend")
    ctx_alpha = _make_ctx_env("alpha", manifest_alpha)
    ctx_beta = _make_ctx_env("beta", manifest_beta)

    combined = _make_dual_repo(fake_repo, fake_repo_beta)
    clock = FakeFollowClock(tick_results=[True])
    sink = io.StringIO()
    svc = LogService(log_repo=combined, follow_clock=clock, tmux=FakeTmuxRepository(), sink=sink)

    events, rc = _run_follow(svc, [(ctx_alpha, _make_query(tail=2)), (ctx_beta, _make_query(tail=2))])

    assert rc == 130
    # Merged set sorted by ts: alpha-1, alpha-2, beta-1, beta-2 → tail=2 → beta-1, beta-2
    assert len(events) == 2
    msgs = [e["msg"] for e in events]
    assert "beta-1" in msgs
    assert "beta-2" in msgs
    assert "alpha-1" not in msgs
    assert "alpha-2" not in msgs


def test_follow_streams_clean_interrupt_returns_130() -> None:
    """Immediate-interrupt clock, two streams → rc 130 and install was called."""
    fake_repo = FakeLogRepository(segments={"backend": []})
    manifest = _make_manifest("backend")
    ctx_alpha = _make_ctx_env("alpha", manifest)
    ctx_beta = _make_ctx_env("beta", manifest)

    clock = FakeFollowClock(tick_results=[True])
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, clock=clock)

    _, rc = _run_follow(svc, [(ctx_alpha, _make_query()), (ctx_beta, _make_query())])

    assert rc == 130
    assert clock.install_called


def test_follow_streams_broken_pipe_returns_0() -> None:
    """Sink raises BrokenPipeError during live follow over two streams → rc 0."""
    fake_repo = FakeLogRepository(segments={"backend": []})
    manifest = _make_manifest("backend")
    ctx_alpha = _make_ctx_env("alpha", manifest)
    ctx_beta = _make_ctx_env("beta", manifest)

    active_alpha = fake_repo.log_path(_WORKTREE_ALPHA, "backend")
    active_beta = fake_repo.log_path(_WORKTREE_BETA, "backend")
    fake_repo.seed_live_content(active_alpha, "")
    fake_repo.seed_live_content(active_beta, "")

    class BrokenSink(io.StringIO):
        def write(self, s: str) -> int:
            raise BrokenPipeError("consumer closed")

    broken = BrokenSink()
    live_appended: list[bool] = []
    tick_count: list[int] = [0]

    class GrowThenBreakClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 1 and not live_appended:
                fake_repo.seed_live_content(active_alpha, "2026-06-15T10:00:01Z\tlive\n")
                live_appended.append(True)
            return False

    clock = GrowThenBreakClock()
    # Construct with broken sink directly; do NOT use _run_follow (it replaces the sink).
    svc = LogService(log_repo=fake_repo, follow_clock=clock, tmux=FakeTmuxRepository(), sink=broken)

    rc = svc.follow_streams(
        [(ctx_alpha, _make_query()), (ctx_beta, _make_query())],
    )

    assert rc == 0


def test_follow_streams_mixed_file_and_pane() -> None:
    """One FILE + one PANE stream: both contribute live lines; PANE events carry no ts."""
    svc_file = Service(name="api", target=Target(window=0, pane=0), command="cmd", log=LogMode.FILE)
    svc_pane = Service(name="shell", target=Target(window=0, pane=1), command="", log=LogMode.PANE)
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(svc_file, svc_pane),
        status_urls=(),
    )
    ctx = _make_ctx_env("alpha", manifest)

    fake_repo = FakeLogRepository(segments={"api": []})
    active_api = fake_repo.log_path(_WORKTREE_ALPHA, "api")
    fake_repo.seed_live_content(active_api, "")

    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.1": 10})
    tmux.capture_text["0.1"] = "pane-line-1\n"

    tick_count: list[int] = [0]

    class MixedGrowClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                fake_repo.seed_live_content(active_api, "2026-06-15T10:00:05Z\tfile-live\n")
                tmux.capture_text["0.1"] = "pane-line-1\npane-live\n"
            return tick_count[0] > 3

    sink = io.StringIO()
    svc = LogService(log_repo=fake_repo, follow_clock=MixedGrowClock(), tmux=tmux, sink=sink)

    query = _make_query()
    events, rc = _run_follow(svc, [(ctx, query)])

    assert rc == 130
    msgs = [e["msg"] for e in events]
    assert "file-live" in msgs
    assert "pane-live" in msgs

    file_ev = next(e for e in events if e["msg"] == "file-live")
    pane_ev = next(e for e in events if e["msg"] == "pane-live")
    assert "ts" in file_ev
    assert "ts" not in pane_ev


def test_follow_streams_install_called_once_sleep_per_tick() -> None:
    """One install() call; one sleep per non-interrupted tick regardless of stream count."""
    fake_repo = FakeLogRepository(segments={"backend": []})
    manifest = _make_manifest("backend")
    ctx_alpha = _make_ctx_env("alpha", manifest)
    ctx_beta = _make_ctx_env("beta", manifest)

    active_alpha = fake_repo.log_path(_WORKTREE_ALPHA, "backend")
    active_beta = fake_repo.log_path(_WORKTREE_BETA, "backend")
    fake_repo.seed_live_content(active_alpha, "")
    fake_repo.seed_live_content(active_beta, "")

    # Two non-interrupted ticks, then interrupt.
    clock = FakeFollowClock(tick_results=[False, False, True])
    sink = io.StringIO()
    svc = _make_svc(fake_repo, sink, clock=clock)

    _run_follow(svc, [(ctx_alpha, _make_query()), (ctx_beta, _make_query())])

    assert clock.install_called
    # install() must only be called once — tracked by a single bool.
    # Two ticks → two sleep calls.
    assert len(clock.sleep_calls) == 2


def test_follow_streams_rotation_resets_offset() -> None:
    """File shrinks mid-follow (rotation/truncation) → offset resets to 0 and new line emitted.

    Ported from the former ``test_follow_rotation_resets_offset`` (which exercised
    ``logs()``-follow, now removed).  Scenario: the active file starts at a large
    offset; on tick 2 the file is replaced with shorter content.  The loop must
    detect the shrink, reset the offset, and emit the new-segment line.
    """
    fake_repo = FakeLogRepository(segments={"backend": []})
    manifest = _make_manifest("backend")
    ctx = _make_ctx_env("alpha", manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE_ALPHA, "backend")
    # Long initial content so offset ends up > len(fresh_content).
    initial_content = "2026-06-15T10:00:01Z\told-line-with-lots-of-extra-content\n"
    fake_repo.seed_live_content(active, initial_content)

    fresh_content = "2026-06-15T10:00:10Z\tnew\n"
    assert len(fresh_content) < len(initial_content)  # sanity: shrink triggers reset

    tick_count: list[int] = [0]

    class RotatingClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                fake_repo.seed_live_content(active, fresh_content)
            return tick_count[0] > 4

    clock = RotatingClock()
    svc = _make_svc(fake_repo, sink, clock=clock)

    events, rc = _run_follow(svc, [(ctx, _make_query())])

    assert rc == 130
    assert any(e["msg"] == "new" for e in events)


def test_follow_streams_seeds_from_backlog_boundary_not_file_end() -> None:
    """A line incomplete at backlog time is emitted by follow once completed.

    Ported from the former ``test_follow_seeds_from_backlog_boundary_not_file_end``
    (which exercised ``logs()``-follow, now removed).  Regression guard for the
    backlog→follow handoff: the follow offset is seeded from the byte boundary
    the backlog read consumed (end of the last complete line), NOT the file end.
    Seeding from file end would skip a trailing partial line even after it is
    completed.  Here the active file holds one complete line plus a partial line
    at follow start; once the partial line gains its newline mid-follow it must
    be emitted exactly once.
    """
    complete = "2026-06-15T10:00:01Z\tcomplete-line\n"
    partial = "2026-06-15T10:00:02Z\tlate-line"  # no trailing newline yet
    fake_repo = FakeLogRepository(segments={"backend": [complete + partial]})
    manifest = _make_manifest("backend")
    ctx = _make_ctx_env("alpha", manifest)
    sink = io.StringIO()

    active = fake_repo.log_path(_WORKTREE_ALPHA, "backend")
    tick_count: list[int] = [0]

    class CompletingClock(FakeFollowClock):
        def interrupted(self) -> bool:
            tick_count[0] += 1
            if tick_count[0] == 2:
                fake_repo.seed_live_content(active, complete + partial + "\n")
            return tick_count[0] > 3

    svc = _make_svc(fake_repo, sink, clock=CompletingClock())

    events, rc = _run_follow(svc, [(ctx, _make_query())])

    assert rc == 130
    msgs = [e["msg"] for e in events]
    # Backlog emits the complete line; follow emits the late line exactly once.
    assert msgs == ["complete-line", "late-line"]


def test_follow_streams_broken_pipe_during_backlog_returns_0() -> None:
    """BrokenPipeError raised during initial backlog emit → rc 0 (no traceback).

    Exercises the ``try/except BrokenPipeError`` wrapping the backlog-write
    loop in ``follow_streams``.  The sink raises immediately on first write;
    the function must catch it and return 0 without entering the follow loop.
    """
    fake_repo = FakeLogRepository(segments={"backend": ["2026-06-15T10:00:01Z\tbacklog-line\n"]})
    manifest = _make_manifest("backend")
    ctx = _make_ctx_env("alpha", manifest)

    class BrokenSink(io.StringIO):
        def write(self, s: str) -> int:
            raise BrokenPipeError("consumer closed during backlog")

    broken = BrokenSink()
    # Clock is never-interrupted (safe default) — BrokenPipeError must exit before the loop.
    clock = FakeFollowClock(tick_results=[False, False, True])
    svc = LogService(log_repo=fake_repo, follow_clock=clock, tmux=FakeTmuxRepository(), sink=broken)

    rc = svc.follow_streams([(ctx, _make_query())])

    assert rc == 0
    # The follow loop must not have started (install not called — BrokenPipe exits before install).
    assert not clock.install_called
