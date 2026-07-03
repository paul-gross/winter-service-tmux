"""Tests for OrchestratorService — full action matrix against hand-rolled fakes.

All four actions are tested: up, down, status, restart.  No subprocess calls
are made; all I/O is mediated through the fakes defined in tests/conftest.py.
"""

from __future__ import annotations

import dataclasses
import io
import shlex
from pathlib import Path

import pytest

import service_manifest.container as sm_container_mod
from service_manifest.modules.manifest.model import (
    Health,
    HealthType,
    LogConfig,
    LogMode,
    Service,
    ServiceManifest,
    Target,
)
from service_orchestrator.core.winter_cli import DependencyStatus, WinterCliUnavailableError
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.health_checker import IHealthChecker
from service_orchestrator.modules.orchestrate.internal.subprocess_health_checker import SubprocessHealthChecker
from service_orchestrator.modules.orchestrate.orchestrator_service import (
    OrchestratorService,
    _dependency_ready,
    _depends_on_timeout_seconds,
    _resolve_service_port,
    _segments_to_prune,
    _topological_order,
)
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.status_report import logwriter_path
from tests.conftest import (
    FakeFollowClock,
    FakeHealthChecker,
    FakeLayoutHookRunner,
    FakeLogRepository,
    FakeProcessReaper,
    FakeTmuxRepository,
    FakeWinterCli,
)
from tests.fakes import FakeFilesystemReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_CONFIG_DIR = _WORKSPACE / ".winter" / "config" / "winter-service-tmux"
_WORKTREE = _WORKSPACE / "alpha"

# A two-service manifest (backend 0.0, frontend 0.1) with a layout hook.
# layout_hook is a bare filename — resolved relative to config_dir at runtime.
_MANIFEST_TOML = """\
session_prefix = "mp"
env_file = ".winter.env"
layout_hook = "layout-hook.sh"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[[service]]
name = "frontend"
target = "0.1"
cmd = "npm run dev"
"""

_MANIFEST_COMMITTED_PATH = Path("config.toml")


def _make_sm_container(toml_content: str = _MANIFEST_TOML) -> sm_container_mod.Container:
    """Build a service_manifest.Container seeded with an in-memory TOML."""
    abs_toml_path = _CONFIG_DIR / _MANIFEST_COMMITTED_PATH
    fake_fs = FakeFilesystemReader({abs_toml_path: toml_content})
    return sm_container_mod.Container(fs=fake_fs)


def _make_manifest(toml_content: str = _MANIFEST_TOML) -> ServiceManifest:
    sm = _make_sm_container(toml_content)
    return sm.manifest_reader.read(_CONFIG_DIR)


def _make_ctx(
    manifest: ServiceManifest | None = None,
    env_vars: dict[str, str] | None = None,
    inject_scope: str | None = "alpha",
    env_file_path: Path | None = None,
) -> SessionContext:
    if manifest is None:
        manifest = _make_manifest()
    return SessionContext(
        env="alpha",
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKTREE,
        config_dir=_CONFIG_DIR,
        session_prefix="mp",
        services=manifest.services,
        layout_hook=manifest.layout_hook,
        logs=manifest.logs,
        env_vars=env_vars,
        inject_scope=inject_scope,
        env_file_path=env_file_path,
    )


def _make_service(
    tmux: FakeTmuxRepository | None = None,
    reaper: FakeProcessReaper | None = None,
    hook_runner: FakeLayoutHookRunner | None = None,
    log_repo: FakeLogRepository | None = None,
    health_checker: IHealthChecker | None = None,
    clock: FakeFollowClock | None = None,
    winter_cli: FakeWinterCli | None = None,
    stdout: io.StringIO | None = None,
    stderr: io.StringIO | None = None,
) -> OrchestratorService:
    if tmux is None:
        tmux = FakeTmuxRepository()
    if reaper is None:
        reaper = FakeProcessReaper()
    if hook_runner is None:
        hook_runner = FakeLayoutHookRunner()
    if log_repo is None:
        log_repo = FakeLogRepository()
    return OrchestratorService(
        tmux=tmux,
        reaper=reaper,
        hook_runner=hook_runner,
        log_repo=log_repo,
        health_checker=health_checker,
        clock=clock,
        winter_cli=winter_cli,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# up — happy path
# ---------------------------------------------------------------------------


def test_up_creates_session_and_runs_hook() -> None:
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook)

    # Seed panes that the hook would have created
    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    result = svc.up(ctx)

    assert result == 0
    assert "mp-alpha" in tmux._sessions
    assert len(hook.calls) == 1
    hook_path, hook_env, hook_cwd = hook.calls[0]
    assert hook_path == _CONFIG_DIR / "layout-hook.sh"
    assert hook_env["WINTER_TMUX_SESSION"] == "mp-alpha"
    assert hook_env["WINTER_TMUX_WORKTREE_DIR"] == str(_WORKTREE)
    assert hook_env["WINTER_ENV"] == "alpha"
    assert hook_cwd == _WORKTREE


def test_up_sends_one_send_keys_per_service() -> None:
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    assert len(tmux.sent) == 2
    sessions = {s for s, _, _ in tmux.sent}
    assert sessions == {"mp-alpha"}
    targets = {t for _, t, _ in tmux.sent}
    assert targets == {"0.0", "0.1"}


def test_up_send_keys_exact_launch_line_with_scope() -> None:
    """Captured services get the writer-wrapped launch line with eval "$(winter env <scope>)"."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()
    ctx = _make_ctx(inject_scope="alpha")
    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    # Both services are captured (log=LogMode.FILE default, non-empty command).
    writer = logwriter_path()
    logs = ctx.logs
    rotate_size = logs.rotate_size_bytes
    max_rot = logs.max_rotations

    # backend line — captured
    backend_line = next(line for _, t, line in tmux.sent if t == "0.0")
    backend_logfile = log_repo.log_path(_WORKTREE, "backend")
    expected_backend = (
        f'cd {shlex.quote(str(_WORKTREE))} && eval "$(winter env alpha)"'
        f" && echo {shlex.quote('=== backend ===')} && "
        f"{{ npm run start:dev ; }} 2>&1 | "
        f"python3 {shlex.quote(str(writer))} {shlex.quote(str(backend_logfile))} "
        f"--rotate-size {rotate_size} --max-rotations {max_rot}"
    )
    assert backend_line == expected_backend

    # frontend line — captured
    frontend_line = next(line for _, t, line in tmux.sent if t == "0.1")
    frontend_logfile = log_repo.log_path(_WORKTREE, "frontend")
    expected_frontend = (
        f'cd {shlex.quote(str(_WORKTREE))} && eval "$(winter env alpha)"'
        f" && echo {shlex.quote('=== frontend ===')} && "
        f"{{ npm run dev ; }} 2>&1 | "
        f"python3 {shlex.quote(str(writer))} {shlex.quote(str(frontend_logfile))} "
        f"--rotate-size {rotate_size} --max-rotations {max_rot}"
    )
    assert frontend_line == expected_frontend

    # ensure_log_dir was called once
    assert log_repo.ensure_log_dir_calls == [_WORKTREE]


def test_up_send_keys_exact_launch_line_without_scope() -> None:
    """Without inject_scope (local mode), no eval prefix; captured service still wrapped."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()
    ctx = _make_ctx(inject_scope=None)
    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    writer = logwriter_path()
    logs = ctx.logs
    rotate_size = logs.rotate_size_bytes
    max_rot = logs.max_rotations
    backend_logfile = log_repo.log_path(_WORKTREE, "backend")

    backend_line = next(line for _, t, line in tmux.sent if t == "0.0")
    expected = (
        f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== backend ===')} && "
        f"{{ npm run start:dev ; }} 2>&1 | "
        f"python3 {shlex.quote(str(writer))} {shlex.quote(str(backend_logfile))} "
        f"--rotate-size {rotate_size} --max-rotations {max_rot}"
    )
    assert backend_line == expected


def test_up_hook_path_is_config_dir_relative() -> None:
    """layout_hook is resolved relative to config_dir, NOT workspace_root."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    hook_path, _, _ = hook.calls[0]
    # Must resolve to config_dir / bare-filename, not workspace_root / path
    assert hook_path == _CONFIG_DIR / "layout-hook.sh"
    assert hook_path != _WORKSPACE / "layout-hook.sh"


def test_up_no_hook_when_layout_hook_is_none() -> None:
    """When manifest.layout_hook is None, hook_runner.run is never called.

    The pane must exist before up() is called (no hook to create it).
    We simulate the tmux new-session creating the initial pane by pre-seeding
    it here — in real tmux, new-session creates window 0 pane 0 automatically.
    """
    toml = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
"""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)
    svc = _make_service(tmux=tmux, hook_runner=hook)

    # Patch new_session to also seed pane 0.0 (what real tmux does).
    _orig_new_session = tmux.new_session

    def _new_session_with_default_pane(session: str, cwd: object, width: object, height: object) -> None:
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session]["0.0"] = 100

    tmux.new_session = _new_session_with_default_pane  # type: ignore[method-assign]

    svc.up(ctx)

    assert hook.calls == []
    assert len(tmux.sent) == 1


# ---------------------------------------------------------------------------
# up — idempotency
# ---------------------------------------------------------------------------


def test_up_idempotent_when_session_exists() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100})
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, stdout=out)

    result = svc.up(ctx)

    assert result == 0
    assert tmux.sent == []
    assert "already running" in out.getvalue()


# ---------------------------------------------------------------------------
# up — salvage: hook fails, ≤1 window → kill + raise
# ---------------------------------------------------------------------------


def test_up_salvage_hook_fail_single_window_kills_and_raises() -> None:
    tmux = FakeTmuxRepository()

    def _add_one_window() -> None:
        # Add one pane in window 0 (the default new-session window) but
        # the hook still fails — so list_windows returns ["0"].
        tmux.seed_session("mp-alpha", {"0.0": 999})

    hook = FakeLayoutHookRunner(
        raise_on_run=OrchestratorError("hook failed"),
        side_effect=_add_one_window,
    )
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook)

    # The side_effect runs before the raise, so we need to set up so that
    # the hook sets up one window (≤1) and then raises.
    # Actually side_effect runs only on success - let's confirm the logic:
    # FakeLayoutHookRunner raises BEFORE calling side_effect.
    # So we need to manually seed the session with 1 window for the salvage check.
    # The service calls new_session (creates empty session), then hook.run raises.
    # list_windows sees the session with 0 panes -> 0 windows -> ≤1 -> kill + raise.

    with pytest.raises(OrchestratorError, match="torn down"):
        svc.up(ctx)

    assert "mp-alpha" not in tmux._sessions
    assert "mp-alpha" in tmux.killed_sessions


def test_up_salvage_hook_fail_multiple_windows_keeps_session() -> None:
    """Hook failure with >1 windows → session kept, non-zero returned, warning printed."""
    tmux = FakeTmuxRepository()

    # Use a custom hook that seeds 2 panes (2 distinct windows) BEFORE raising,
    # so that list_windows returns >1 window when the salvage check runs.
    class _HookWithPreRaiseSideEffect(FakeLayoutHookRunner):
        def run(self, hook_path: Path, env: dict[str, str], cwd: Path) -> None:
            # Seed 2 windows before raising so the salvage heuristic sees >1 window.
            tmux.seed_session("mp-alpha", {"0.0": 100, "1.0": 101})
            raise OrchestratorError("hook partial fail")

    hook = _HookWithPreRaiseSideEffect()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, hook_runner=hook, stdout=out)

    result = svc.up(ctx)

    assert result == 1
    assert "mp-alpha" in tmux._sessions
    assert "mp-alpha" not in tmux.killed_sessions
    assert "Warning" in out.getvalue()


# ---------------------------------------------------------------------------
# up — pane validation: missing target → kill + raise
# ---------------------------------------------------------------------------


def test_up_missing_pane_after_hook_raises() -> None:
    """After the hook, if a manifest target has no matching pane → OrchestratorError."""
    tmux = FakeTmuxRepository()
    # Hook succeeds but only adds pane 0.0; manifest also needs 0.1.

    def _add_one_pane() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100})

    hook = FakeLayoutHookRunner(side_effect=_add_one_pane)
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook)

    with pytest.raises(OrchestratorError, match="manifest targets not found"):
        svc.up(ctx)


# ---------------------------------------------------------------------------
# down — reaps descendants then kills session
# ---------------------------------------------------------------------------


def test_down_reaps_all_descendants_then_kills_session() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(
        descendant_map={10: [100, 101], 20: [200]},
    )
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.down(ctx)

    assert result == 0
    assert "mp-alpha" not in tmux._sessions
    assert len(reaper.killed) == 1
    killed_pids = reaper.killed[0]
    assert set(killed_pids) == {100, 101, 200}


def test_down_noop_when_no_session() -> None:
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    result = svc.down(ctx)

    assert result == 0
    assert reaper.killed == []
    assert "No running session" in out.getvalue()


def test_down_skips_reap_when_no_descendants() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    reaper = FakeProcessReaper(descendant_map={10: []})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    svc.down(ctx)

    assert reaper.killed == []
    assert "mp-alpha" not in tmux._sessions


# ---------------------------------------------------------------------------
# up / down — partial (service-segment filtered) start/stop
# ---------------------------------------------------------------------------

# A three-service manifest so a two-name partial request is unambiguously a
# genuine subset (never accidentally "covers everything").
_MANIFEST_THREE_SERVICES_TOML = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[[service]]
name = "frontend"
target = "0.1"
cmd = "npm run dev"

[[service]]
name = "worker"
target = "0.2"
cmd = "npm run worker"
"""


def _make_three_services_ctx() -> SessionContext:
    manifest = _make_manifest(_MANIFEST_THREE_SERVICES_TOML)
    return _make_ctx(manifest=manifest, inject_scope=None)


def test_up_partial_session_absent_creates_and_launches_only_matched() -> None:
    """Session absent + partial services=("backend",): session/hook created, only backend launched."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    result = svc.up(ctx, services=("backend",))

    assert result == 0
    assert "mp-alpha" in tmux._sessions
    assert len(hook.calls) == 1
    # Only backend's pane (0.0) got a launch line; frontend (0.1) untouched.
    assert [t for _, t, _ in tmux.sent] == ["0.0"]


def test_up_partial_session_present_launches_only_not_running_matched() -> None:
    """Session present: matched-but-running service skipped, matched-not-running launched.

    Matched set is ("backend", "frontend") — a genuine subset, since the
    manifest also declares "worker" — so this exercises the partial (not
    whole-scope) path.
    """
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101, "0.2": 102})
    # backend (100) not running; frontend (101) already running; worker (102) unmatched.
    reaper = FakeProcessReaper(children_set={101})
    ctx = _make_three_services_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.up(ctx, services=("backend", "frontend"))

    assert result == 0
    # Only backend's pane got a launch line — frontend was already running,
    # worker was never a candidate at all.
    assert [t for _, t, _ in tmux.sent] == ["0.0"]
    # Session was never recreated.
    assert tmux.created_sessions == []


def test_up_partial_unmatched_service_never_touched() -> None:
    """A service not named in *services* is never sent a launch line, matched or not."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})
    reaper = FakeProcessReaper()  # neither pane has children
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.up(ctx, services=("backend",))

    assert result == 0
    assert [t for _, t, _ in tmux.sent] == ["0.0"]


def test_up_star_or_full_coverage_service_segment_is_whole_scope_idempotent() -> None:
    """services covering every declared name behaves exactly like whole-scope up (idempotent)."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, stdout=out)

    result = svc.up(ctx, services=("backend", "frontend"))

    assert result == 0
    assert tmux.sent == []
    assert "already running" in out.getvalue()


def test_up_no_session_and_full_coverage_services_behaves_like_whole_scope() -> None:
    """services covering every declared name, with no session yet, launches every service."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    result = svc.up(ctx, services=("backend", "frontend"))

    assert result == 0
    assert {t for _, t, _ in tmux.sent} == {"0.0", "0.1"}


def test_up_partial_missing_pane_in_existing_session_is_skipped_not_raised() -> None:
    """A matched service with no live pane in an already-running session is skipped, no raise.

    Matched set is ("backend", "frontend") against a three-service manifest,
    so this remains a genuine partial request even though frontend's pane is
    absent.
    """
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100, "0.2": 102})  # frontend's 0.1 pane absent
    ctx = _make_three_services_ctx()
    svc = _make_service(tmux=tmux)

    result = svc.up(ctx, services=("backend", "frontend"))

    assert result == 0
    assert [t for _, t, _ in tmux.sent] == ["0.0"]


_MANIFEST_PARTIAL_STARTUP_TOML = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.startup]
retries = 2
retry_delay = 1.0

[[service]]
name = "frontend"
target = "0.1"
cmd = "npm run dev"
"""


def test_up_partial_existing_session_retry_monitors_only_matched() -> None:
    """retry=True on a partial up only monitors/re-launches the matched, actually-launched subset."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 200})
    manifest = _make_manifest(_MANIFEST_PARTIAL_STARTUP_TOML)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)
    # First call = "already running?" pre-check (False, launch); second = settle
    # check (False, dead); third = post-retry check (True, alive).
    reaper = FakeProcessReaper(children_sequence={100: [False, False, True]})
    clock = FakeFollowClock()
    svc = _make_service(tmux=tmux, reaper=reaper, clock=clock)

    result = svc.up(ctx, retry=True, services=("backend",))

    assert result == 0
    # Initial send + one retry send, both to backend's pane; frontend untouched.
    assert [t for _, t, _ in tmux.sent] == ["0.0", "0.0"]
    assert clock.sleep_calls == [0.5, 1.0]


def test_down_partial_reaps_only_matched_leaves_session_and_others_running() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(descendant_map={10: [100], 20: [200]})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.down(ctx, services=("backend",))

    assert result == 0
    # Only backend's (0.0/pid 10) descendants were reaped.
    assert reaper.killed == [[100]]
    # Session survives; frontend's pane is untouched.
    assert "mp-alpha" in tmux._sessions


def test_down_partial_no_session_is_still_a_noop() -> None:
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    result = svc.down(ctx, services=("backend",))

    assert result == 0
    assert reaper.killed == []
    assert "No running session" in out.getvalue()


def test_down_full_coverage_services_behaves_like_whole_scope() -> None:
    """services naming every declared service tears down the whole session, like bare scope."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(descendant_map={10: [100], 20: [200]})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.down(ctx, services=("backend", "frontend"))

    assert result == 0
    assert "mp-alpha" not in tmux._sessions
    assert len(reaper.killed) == 1
    assert set(reaper.killed[0]) == {100, 200}


# ---------------------------------------------------------------------------
# status — running / stopped / missing
# ---------------------------------------------------------------------------


def test_status_running_service() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    tmux.capture_text = {"0.0": "Starting server...\nListening on :3000\n"}
    reaper = FakeProcessReaper(children_set={10})  # 10 has children → running
    ctx = _make_ctx(env_vars={"BACKEND_PORT": "3000"})
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    result = svc.status(ctx)

    assert result == 0
    output = out.getvalue()
    assert "backend:" in output
    assert "running" in output
    assert "Listening on :3000" in output


def test_status_stopped_service() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(children_set=set())  # no children → stopped
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    result = svc.status(ctx)

    assert result == 0
    assert "stopped" in out.getvalue()


def test_status_missing_pane() -> None:
    tmux = FakeTmuxRepository()
    # Only pane 0.0 exists; manifest also declares 0.1
    tmux.seed_session("mp-alpha", {"0.0": 10})
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    result = svc.status(ctx)

    assert result == 0
    output = out.getvalue()
    assert "missing" in output
    assert "frontend" in output


def test_status_renders_health_column_when_probe_declared() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    tmux.capture_text = {"0.0": "backend ready\n", "0.1": "frontend booted\n"}
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="cmd",
                health=Health(type=HealthType.URL, target="http://localhost:${BACKEND_PORT}/health"),
            ),
            Service(name="frontend", target=Target(0, 1), cmd="cmd"),
        ),
    )
    ctx = _make_ctx(manifest=manifest, env_vars={"BACKEND_PORT": "3000"})
    health = FakeHealthChecker({"http://localhost:${BACKEND_PORT}/health": True})
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=FakeProcessReaper(children_set={10, 20}), health_checker=health, stdout=out)

    result = svc.status(ctx)

    assert result == 0
    output = out.getvalue()
    assert "backend:" in output
    assert "healthy" in output
    assert "frontend:" in output
    assert "-" in output


def test_status_no_session() -> None:
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    result = svc.status(ctx)

    assert result == 0
    assert "No mp-alpha session running" in out.getvalue()


# ---------------------------------------------------------------------------
# restart — reaps target pane children + re-sends
# ---------------------------------------------------------------------------


def test_restart_reaps_children_and_resends() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(descendant_map={10: [100, 101]})
    ctx = _make_ctx(inject_scope="alpha")
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.restart(ctx, "backend")

    assert result == 0
    assert reaper.killed == [[100, 101]]

    # Re-send line
    assert len(tmux.sent) == 1
    session, target, line = tmux.sent[0]
    assert session == "mp-alpha"
    assert target == "0.0"
    expected = (
        f'cd {shlex.quote(str(_WORKTREE))} && eval "$(winter env alpha)"'
        f" && echo {shlex.quote('=== backend ===')} && npm run start:dev"
    )
    assert line == expected


def test_restart_no_children_is_noop_reap() -> None:
    """A stopped service (no children) restarts without reaping."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(descendant_map={10: []})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    svc.restart(ctx, "backend")

    assert reaper.killed == []
    assert len(tmux.sent) == 1


def test_restart_unknown_service_lists_declared_names() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux)

    with pytest.raises(OrchestratorError, match="unknown service 'worker'"):
        svc.restart(ctx, "worker")

    with pytest.raises(OrchestratorError, match="backend"):
        svc.restart(ctx, "worker")


def test_restart_missing_pane_raises() -> None:
    """When the declared target pane is absent from the session → OrchestratorError."""
    tmux = FakeTmuxRepository()
    # Only 0.0 exists; backend is at 0.0, frontend at 0.1
    tmux.seed_session("mp-alpha", {"0.0": 10})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux)

    with pytest.raises(OrchestratorError, match="not found"):
        svc.restart(ctx, "frontend")


# ---------------------------------------------------------------------------
# restart — shell pane excluded (descendants call uses pane pid, not itself)
# ---------------------------------------------------------------------------


def test_restart_only_reaps_children_not_shell() -> None:
    """reaper.descendants(pane_pid) is called, not reaper.descendants excluding shell."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    # Pane pid is 10; its children are 100, 101 (the shell itself is 10,
    # which descendants() already excludes per the Protocol contract).
    reaper = FakeProcessReaper(descendant_map={10: [100, 101]})

    # A single-service manifest
    toml = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "cmd"
"""
    manifest = _make_manifest(toml)
    ctx2 = _make_ctx(manifest=manifest, inject_scope=None)
    svc2 = _make_service(tmux=tmux, reaper=reaper)

    svc2.restart(ctx2, "backend")

    assert reaper.killed == [[100, 101]]


# ---------------------------------------------------------------------------
# up — empty command (banner-only / shell pane)
# ---------------------------------------------------------------------------


def test_up_empty_command_sends_banner_only() -> None:
    """Empty command → banner only line sent; no trailing '&& <cmd>'."""
    toml = """\
session_prefix = "mp"

[[service]]
name = "shell"
target = "0.0"
cmd = ""
"""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)

    # No layout_hook declared → hook never called → seed pane via new_session patch.
    _orig_new_session = tmux.new_session

    def _new_session_with_pane(session: str, cwd: object, width: object, height: object) -> None:
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session]["0.0"] = 100

    tmux.new_session = _new_session_with_pane  # type: ignore[method-assign]

    svc = _make_service(tmux=tmux, hook_runner=hook)
    svc.up(ctx)

    assert len(tmux.sent) == 1
    _, _, line = tmux.sent[0]
    assert line == f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== shell ===')} ".strip()


# ---------------------------------------------------------------------------
# up — capture writer integration: captured, bare (empty-command), log=False
# ---------------------------------------------------------------------------


def test_up_captured_service_gets_writer_wrapped_line() -> None:
    """Non-empty command with log=True is wrapped through the capture writer."""
    toml = """\
session_prefix = "mp"

[[service]]
name = "docs"
target = "0.0"
cmd = "npm run docs"

[logs]
rotate_size_bytes = 5242880
max_rotations = 3
"""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)

    _orig_new_session = tmux.new_session

    def _new_session_with_pane(session: str, cwd: object, width: object, height: object) -> None:
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session]["0.0"] = 100

    tmux.new_session = _new_session_with_pane  # type: ignore[method-assign]

    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo)
    svc.up(ctx)

    writer = logwriter_path()
    logfile = log_repo.log_path(_WORKTREE, "docs")
    expected = (
        f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== docs ===')} && "
        f"{{ npm run docs ; }} 2>&1 | "
        f"python3 {shlex.quote(str(writer))} {shlex.quote(str(logfile))} "
        f"--rotate-size 5242880 --max-rotations 3"
    )
    _, _, line = tmux.sent[0]
    assert line == expected
    assert log_repo.ensure_log_dir_calls == [_WORKTREE]


def test_up_pane_mode_service_gets_bare_line() -> None:
    """Service with log='pane' skips capture wrapping; bare launch line is sent."""
    toml = """\
session_prefix = "mp"

[[service]]
name = "worker"
target = "0.0"
cmd = "python -m worker"
log = "pane"
"""
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)

    _orig_new_session = tmux.new_session

    def _new_session_with_pane(session: str, cwd: object, width: object, height: object) -> None:
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session]["0.0"] = 100

    tmux.new_session = _new_session_with_pane  # type: ignore[method-assign]

    svc = _make_service(tmux=tmux, log_repo=log_repo)
    svc.up(ctx)

    _, _, line = tmux.sent[0]
    expected = f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== worker ===')} && python -m worker"
    assert line == expected
    # ensure_log_dir is still called once regardless
    assert log_repo.ensure_log_dir_calls == [_WORKTREE]


def test_up_memory_mode_service_gets_bare_line() -> None:
    """Service with log='memory' skips capture wrapping; bare launch line is sent."""
    toml = """\
session_prefix = "mp"

[[service]]
name = "worker"
target = "0.0"
cmd = "python -m worker"
log = "memory"
"""
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)

    _orig_new_session = tmux.new_session

    def _new_session_with_pane(session: str, cwd: object, width: object, height: object) -> None:
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session]["0.0"] = 100

    tmux.new_session = _new_session_with_pane  # type: ignore[method-assign]

    svc = _make_service(tmux=tmux, log_repo=log_repo)
    svc.up(ctx)

    _, _, line = tmux.sent[0]
    expected = f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== worker ===')} && python -m worker"
    assert line == expected


def test_up_file_mode_with_command_gets_writer_wrapped_line() -> None:
    """log=FILE + non-empty command → writer-wrapped launch line."""
    toml = """\
session_prefix = "mp"

[[service]]
name = "api"
target = "0.0"
cmd = "python -m api"
log = "file"
"""
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)

    _orig_new_session = tmux.new_session

    def _new_session_with_pane(session: str, cwd: object, width: object, height: object) -> None:
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session]["0.0"] = 100

    tmux.new_session = _new_session_with_pane  # type: ignore[method-assign]

    svc = _make_service(tmux=tmux, log_repo=log_repo)
    svc.up(ctx)

    _, _, line = tmux.sent[0]
    assert "python3" in line
    assert "logwriter" in line


def test_up_ensure_log_dir_called_once() -> None:
    """ensure_log_dir is called exactly once per up() regardless of service count."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()
    ctx = _make_ctx()

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo)
    svc.up(ctx)

    assert len(log_repo.ensure_log_dir_calls) == 1
    assert log_repo.ensure_log_dir_calls[0] == _WORKTREE


# ---------------------------------------------------------------------------
# Prune helpers
# ---------------------------------------------------------------------------


def _make_prune_manifest(retention_seconds: int = 604800) -> ServiceManifest:
    """Build a minimal single-service manifest for prune tests."""
    return ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(Service(name="docs", target=Target(window=0, pane=0), cmd="cmd"),),
        logs=LogConfig(retention_seconds=retention_seconds),
    )


def _make_prune_ctx(retention_seconds: int = 604800) -> SessionContext:
    manifest = _make_prune_manifest(retention_seconds)
    return SessionContext(
        env="alpha",
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKTREE,
        config_dir=_CONFIG_DIR,
        session_prefix="mp",
        services=manifest.services,
        layout_hook=manifest.layout_hook,
        logs=manifest.logs,
        env_vars=None,
        inject_scope=None,
        env_file_path=None,
    )


# ---------------------------------------------------------------------------
# _segments_to_prune — pure helper unit tests
# ---------------------------------------------------------------------------


def test_segments_to_prune_returns_old_segments() -> None:
    """Segments with mtime < cutoff are returned."""
    log_repo = FakeLogRepository()
    old_seg = Path("/logs/docs.log.1")
    log_repo.seed_mtime(old_seg, 1000.0)
    result = _segments_to_prune([old_seg], log_repo, cutoff=2000.0)
    assert result == [old_seg]


def test_segments_to_prune_keeps_recent_segments() -> None:
    """Segments with mtime >= cutoff are not returned."""
    log_repo = FakeLogRepository()
    recent_seg = Path("/logs/docs.log.1")
    log_repo.seed_mtime(recent_seg, 3000.0)
    result = _segments_to_prune([recent_seg], log_repo, cutoff=2000.0)
    assert result == []


def test_segments_to_prune_mixed() -> None:
    """Only segments older than cutoff are returned."""
    log_repo = FakeLogRepository()
    old_seg = Path("/logs/docs.log.2")
    recent_seg = Path("/logs/docs.log.1")
    log_repo.seed_mtime(old_seg, 500.0)
    log_repo.seed_mtime(recent_seg, 3000.0)
    result = _segments_to_prune([old_seg, recent_seg], log_repo, cutoff=2000.0)
    assert result == [old_seg]


# ---------------------------------------------------------------------------
# prune() — session guard, deletion, active-file safety
# ---------------------------------------------------------------------------


def test_prune_deletes_old_rotated_segment_when_session_not_running() -> None:
    """Old rotated segment is deleted when the session is not running.

    now=700000, retention=604800 → cutoff=95200; segment mtime=1 < 95200 → deleted.
    """
    tmux = FakeTmuxRepository()  # no session seeded → has_session returns False
    log_repo = FakeLogRepository()
    clock = FakeFollowClock(current_time=700000.0)
    seg = Path("/fake/workspace/alpha/.winter/logs/docs.log.1")
    log_repo.seed_rotated_segments("docs", [seg])
    log_repo.seed_mtime(seg, 1.0)  # very old: mtime=1 < cutoff=95200

    ctx = _make_prune_ctx(retention_seconds=604800)
    svc = OrchestratorService(
        tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock
    )
    svc._prune(ctx)

    assert seg in log_repo.deleted


def test_prune_skips_deletion_when_session_is_running() -> None:
    """Prune does nothing when the env session is already running.

    Even with an old segment (mtime=1) and now=700000 → cutoff=95200 (which
    would normally trigger deletion), the has_session guard fires first.
    """
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100})  # session IS running
    log_repo = FakeLogRepository()
    clock = FakeFollowClock(current_time=700000.0)
    seg = Path("/fake/workspace/alpha/.winter/logs/docs.log.1")
    log_repo.seed_rotated_segments("docs", [seg])
    log_repo.seed_mtime(seg, 1.0)  # old — would be deleted if session not running

    ctx = _make_prune_ctx()
    svc = OrchestratorService(
        tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock
    )
    svc._prune(ctx)

    assert log_repo.deleted == []


def test_prune_keeps_recent_segment() -> None:
    """Segment within retention window is not deleted.

    now=700000, retention=604800 → cutoff=95200; segment mtime=500000 > 95200 → kept.
    """
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    clock = FakeFollowClock(current_time=700000.0)
    seg = Path("/fake/workspace/alpha/.winter/logs/docs.log.1")
    log_repo.seed_rotated_segments("docs", [seg])
    log_repo.seed_mtime(seg, 500000.0)  # within retention window

    ctx = _make_prune_ctx(retention_seconds=604800)
    svc = OrchestratorService(
        tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock
    )
    svc._prune(ctx)

    assert log_repo.deleted == []


def test_prune_active_log_never_deleted() -> None:
    """The active <svc>.log is never returned by rotated_segments, so prune cannot delete it.

    rotated_segments only returns .log.N files; the active .log is excluded by
    design.  This test verifies the fake correctly excludes the active log and
    that prune never touches it.
    """
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    clock = FakeFollowClock(current_time=10000.0)
    # rotated_segments returns only rotated files (no active .log)
    active_log = Path("/fake/workspace/alpha/.winter/logs/docs.log")
    log_repo.seed_rotated_segments("docs", [])  # no rotated segs
    log_repo.seed_mtime(active_log, 1.0)  # would be prunable if eligible

    ctx = _make_prune_ctx()
    svc = OrchestratorService(
        tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock
    )
    svc._prune(ctx)

    assert active_log not in log_repo.deleted
    assert log_repo.deleted == []


# ---------------------------------------------------------------------------
# prune-on-up: ordering and failure-safety
# ---------------------------------------------------------------------------


def test_up_calls_prune_before_new_session() -> None:
    """prune() runs before new_session() in up().

    We verify ordering by recording the sequence of calls:
    - prune checks rotated_segments (called on log_repo)
    - new_session is called on tmux
    Any segment deletion must precede session creation.

    now=700000, retention=604800 → cutoff=95200; segs mtime=1 < 95200 → deleted.
    """
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    clock = FakeFollowClock(current_time=700000.0)
    # Seed old rotated segments for the manifest services (backend + frontend).
    seg_backend = Path("/fake/workspace/alpha/.winter/logs/backend.log.1")
    seg_frontend = Path("/fake/workspace/alpha/.winter/logs/frontend.log.1")
    log_repo.seed_rotated_segments("backend", [seg_backend])
    log_repo.seed_rotated_segments("frontend", [seg_frontend])
    log_repo.seed_mtime(seg_backend, 1.0)
    log_repo.seed_mtime(seg_frontend, 1.0)

    call_order: list[str] = []
    _orig_delete = log_repo.delete
    _orig_new_session = tmux.new_session

    def _tracked_delete(path: Path) -> None:
        call_order.append(f"delete:{path.name}")
        _orig_delete(path)

    def _tracked_new_session(session: str, cwd: object, width: object, height: object) -> None:
        call_order.append("new_session")
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session]["0.0"] = 100
        tmux._sessions[session]["0.1"] = 101

    log_repo.delete = _tracked_delete  # type: ignore[method-assign]
    tmux.new_session = _tracked_new_session  # type: ignore[method-assign]

    hook = FakeLayoutHookRunner()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo, clock=clock)
    result = svc.up(ctx)

    assert result == 0
    # All deletes must come before new_session
    new_session_idx = call_order.index("new_session")
    for entry in call_order:
        if entry.startswith("delete:"):
            assert call_order.index(entry) < new_session_idx


def test_up_prune_failure_does_not_block_up() -> None:
    """A prune error is swallowed; up() still creates the session and returns 0."""

    class _BrokenLogRepo(FakeLogRepository):
        def rotated_segments(self, worktree_dir: Path, service: str) -> list[Path]:
            raise RuntimeError("disk error")

    tmux = FakeTmuxRepository()
    log_repo = _BrokenLogRepo()
    clock = FakeFollowClock(current_time=10000.0)
    hook = FakeLayoutHookRunner()

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo, clock=clock)
    result = svc.up(ctx)

    assert result == 0
    assert "mp-alpha" in tmux._sessions


# ---------------------------------------------------------------------------
# _segments_to_prune — vanished segment mid-prune does not abort the pass
# ---------------------------------------------------------------------------


def test_segments_to_prune_skips_vanished_segment() -> None:
    """A segment that disappears between listing and mtime read is silently skipped.

    The race window: rotated_segments returns a path, but by the time mtime()
    is called the file has been removed.  _segments_to_prune must catch the
    resulting FileNotFoundError and continue with remaining segments.
    """

    class _RacingLogRepository(FakeLogRepository):
        """Raises FileNotFoundError for one specific path to simulate the race."""

        def __init__(self, vanished: Path) -> None:
            super().__init__()
            self._vanished = vanished

        def mtime(self, path: Path) -> float:
            if path == self._vanished:
                raise FileNotFoundError(f"simulated race: {path} vanished")
            return super().mtime(path)

    vanished_seg = Path("/logs/docs.log.2")
    surviving_seg = Path("/logs/docs.log.1")

    repo = _RacingLogRepository(vanished=vanished_seg)
    repo.seed_mtime(surviving_seg, 1.0)  # old enough to prune

    # _segments_to_prune must skip the vanished segment and still return the
    # surviving old segment — the pass must not abort.
    result = _segments_to_prune([vanished_seg, surviving_seg], repo, cutoff=2000.0)

    assert vanished_seg not in result
    assert surviving_seg in result


# ---------------------------------------------------------------------------
# status_env_document — winter's env-keyed status fragment
# ---------------------------------------------------------------------------


def test_status_env_document_session_not_running() -> None:
    """No session → every declared service is 'stopped' with no live handle.

    The session name is still reported (a stable identity), and log_path is
    still resolvable from the manifest even when nothing is running.
    """
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    doc = svc.status_env_document(ctx)

    assert doc["env"] == "alpha"
    assert doc["session"] == "mp-alpha"
    states = {s["name"]: s["state"] for s in doc["services"]}
    assert states == {"backend": "stopped", "frontend": "stopped"}
    handles = {s["name"]: s["handle"] for s in doc["services"]}
    assert handles == {"backend": None, "frontend": None}
    log_paths = {s["name"]: s["log_path"] for s in doc["services"]}
    assert log_paths["backend"] == str(_WORKTREE / ".winter" / "logs" / "backend.log")


def test_status_env_document_running_and_stopped_states() -> None:
    """state='running' when the pane has children, 'stopped' when it does not;
    handle is the live pane address."""
    tmux = FakeTmuxRepository()
    # backend (0.0, pid=10) has children → running; frontend (0.1, pid=20) → stopped
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(children_set={10})  # only pid 10 has children
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    doc = svc.status_env_document(ctx)

    by_name = {s["name"]: s for s in doc["services"]}
    assert by_name["backend"]["state"] == "running"
    assert by_name["backend"]["handle"] == "mp-alpha:0.0"
    assert by_name["frontend"]["state"] == "stopped"
    assert by_name["frontend"]["handle"] == "mp-alpha:0.1"


def test_status_env_document_missing_pane_is_stopped() -> None:
    """A pane absent from a running session reports state='stopped', handle=None."""
    tmux = FakeTmuxRepository()
    # Only pane 0.0 present; manifest declares both 0.0 (backend) and 0.1 (frontend)
    tmux.seed_session("mp-alpha", {"0.0": 10})
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    doc = svc.status_env_document(ctx)

    frontend = next(s for s in doc["services"] if s["name"] == "frontend")
    assert frontend["state"] == "stopped"
    assert frontend["handle"] is None


def test_status_env_document_shape_stability() -> None:
    """Every contract field is present; unknown enums/scalars use 'unknown'/null/[]."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(children_set={10})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    doc = svc.status_env_document(ctx)

    assert set(doc.keys()) == {"env", "session", "port_base", "services"}
    for s in doc["services"]:
        assert set(s.keys()) == {"name", "state", "health", "ports", "handle", "log_path", "since"}
        assert s["health"] == "unknown"
        assert s["ports"] == []
        assert s["since"] is None


def test_status_env_document_populates_declared_health() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="cmd",
                health=Health(type=HealthType.URL, target="http://localhost:${BACKEND_PORT}/health"),
            ),
            Service(
                name="frontend",
                target=Target(0, 1),
                cmd="cmd",
                health=Health(type=HealthType.CMD, target="pgrep -f frontend"),
            ),
            Service(name="shell", target=Target(1, 0), cmd=""),
        ),
    )
    ctx = _make_ctx(manifest=manifest, env_vars={"BACKEND_PORT": "3000"})
    health = FakeHealthChecker(
        {
            "http://localhost:${BACKEND_PORT}/health": True,
            "pgrep -f frontend": False,
        }
    )
    svc = _make_service(
        tmux=tmux, reaper=FakeProcessReaper(children_set={10, 20}), health_checker=health, stdout=io.StringIO()
    )

    doc = svc.status_env_document(ctx)

    by_name = {s["name"]: s for s in doc["services"]}
    assert by_name["backend"]["health"] == "healthy"
    assert by_name["frontend"]["health"] == "unhealthy"
    assert by_name["shell"]["health"] == "unknown"
    assert health.calls == [
        ("http://localhost:${BACKEND_PORT}/health", {"BACKEND_PORT": "3000"}, _WORKTREE, None, None),
        ("pgrep -f frontend", {"BACKEND_PORT": "3000"}, _WORKTREE, None, None),
    ]


def test_status_env_document_declared_health_is_unhealthy_without_running_service() -> None:
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="cmd",
                health=Health(type=HealthType.URL, target="http://localhost:${BACKEND_PORT}/health"),
            ),
        ),
    )
    ctx = _make_ctx(manifest=manifest, env_vars={"BACKEND_PORT": "3000"})
    health = FakeHealthChecker({"http://localhost:${BACKEND_PORT}/health": True})
    svc = _make_service(tmux=FakeTmuxRepository(), reaper=FakeProcessReaper(), health_checker=health)

    doc = svc.status_env_document(ctx)

    service = doc["services"][0]
    assert service["state"] == "stopped"
    assert service["health"] == "unhealthy"
    assert health.calls == []


def test_status_env_document_declared_health_is_unhealthy_for_stopped_pane() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="cmd",
                health=Health(type=HealthType.CMD, target="true"),
            ),
        ),
    )
    ctx = _make_ctx(manifest=manifest)
    health = FakeHealthChecker({"true": True})
    svc = _make_service(tmux=tmux, reaper=FakeProcessReaper(children_set=set()), health_checker=health)

    doc = svc.status_env_document(ctx)

    service = doc["services"][0]
    assert service["state"] == "stopped"
    assert service["health"] == "unhealthy"
    assert health.calls == []


def test_status_env_document_declared_health_is_unhealthy_for_missing_pane() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="cmd",
                health=Health(type=HealthType.CMD, target="true"),
            ),
        ),
    )
    ctx = _make_ctx(manifest=manifest)
    health = FakeHealthChecker({"true": True})
    svc = _make_service(tmux=tmux, reaper=FakeProcessReaper(children_set={10}), health_checker=health)

    doc = svc.status_env_document(ctx)

    service = doc["services"][0]
    assert service["state"] == "stopped"
    assert service["health"] == "unhealthy"
    assert health.calls == []


def test_status_env_document_log_health_file_mode_healthy_when_pattern_present() -> None:
    """A FILE-mode 'log' probe is healthy once the ready-line appears in the captured log tail."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="npm start",
                health=Health(type=HealthType.LOG, target=r"Listening on port \d+"),
            ),
        ),
    )
    ctx = _make_ctx(manifest=manifest)
    log_repo = FakeLogRepository()
    svc = _make_service(
        tmux=tmux,
        reaper=FakeProcessReaper(children_set={10}),
        log_repo=log_repo,
        health_checker=SubprocessHealthChecker(),
    )
    log_repo.seed_live_content(log_repo.log_path(_WORKTREE, "backend"), "booting...\nListening on port 3000\n")

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "healthy"


def test_status_env_document_log_health_file_mode_unhealthy_when_pattern_absent() -> None:
    """The same FILE-mode probe is unhealthy while the ready-line has not appeared yet."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="npm start",
                health=Health(type=HealthType.LOG, target=r"Listening on port \d+"),
            ),
        ),
    )
    ctx = _make_ctx(manifest=manifest)
    log_repo = FakeLogRepository()
    svc = _make_service(
        tmux=tmux,
        reaper=FakeProcessReaper(children_set={10}),
        log_repo=log_repo,
        health_checker=SubprocessHealthChecker(),
    )
    log_repo.seed_live_content(log_repo.log_path(_WORKTREE, "backend"), "booting...\nstill starting up\n")

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "unhealthy"


def test_status_env_document_log_health_pane_mode_scans_capture_pane() -> None:
    """A PANE-mode 'log' probe matches against the live tmux pane buffer, not the log file."""
    tmux = FakeTmuxRepository(capture_text={"0.0": "booting...\nready to accept connections\n"})
    tmux.seed_session("mp-alpha", {"0.0": 10})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(
            Service(
                name="shell",
                target=Target(0, 0),
                cmd="my-interactive-tool",
                log=LogMode.PANE,
                health=Health(type=HealthType.LOG, target="ready to accept connections"),
            ),
        ),
    )
    ctx = _make_ctx(manifest=manifest)
    # A file-mode log repo seeded with content that would NOT match, proving
    # the pane-mode probe reads capture_pane rather than the log file.
    log_repo = FakeLogRepository()
    log_repo.seed_live_content(log_repo.log_path(_WORKTREE, "shell"), "not a match\n")
    svc = _make_service(
        tmux=tmux,
        reaper=FakeProcessReaper(children_set={10}),
        log_repo=log_repo,
        health_checker=SubprocessHealthChecker(),
    )

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "healthy"


def _uptime_manifest(duration: str = "30s") -> ServiceManifest:
    return ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(
            Service(
                name="backend",
                target=Target(0, 0),
                cmd="npm start",
                health=Health(type=HealthType.UPTIME, target=duration),
            ),
        ),
    )


def test_status_env_document_uptime_health_below_threshold_is_unhealthy() -> None:
    """A running child that hasn't been alive as long as the declared duration is unhealthy."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    ctx = _make_ctx(manifest=_uptime_manifest("30s"))
    svc = _make_service(
        tmux=tmux,
        reaper=FakeProcessReaper(children_set={10}, child_uptimes={10: 5}),
        health_checker=SubprocessHealthChecker(),
    )

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "unhealthy"


def test_status_env_document_uptime_health_at_threshold_is_healthy() -> None:
    """A running child alive exactly as long as the declared duration is healthy."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    ctx = _make_ctx(manifest=_uptime_manifest("30s"))
    svc = _make_service(
        tmux=tmux,
        reaper=FakeProcessReaper(children_set={10}, child_uptimes={10: 30}),
        health_checker=SubprocessHealthChecker(),
    )

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "healthy"


def test_status_env_document_uptime_health_above_threshold_is_healthy() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    ctx = _make_ctx(manifest=_uptime_manifest("30s"))
    svc = _make_service(
        tmux=tmux,
        reaper=FakeProcessReaper(children_set={10}, child_uptimes={10: 300}),
        health_checker=SubprocessHealthChecker(),
    )

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "healthy"


def test_status_env_document_uptime_health_no_child_pane_is_unhealthy() -> None:
    """A running pane whose child cannot be resolved for uptime (edge race, or an
    interactive pane where the reaper finds no measurable child) is unhealthy."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    ctx = _make_ctx(manifest=_uptime_manifest("30s"))
    svc = _make_service(
        tmux=tmux,
        # has_children() reports the pane running, but child_uptime_seconds()
        # has nothing seeded for pid 10 -> None -> unhealthy.
        reaper=FakeProcessReaper(children_set={10}, child_uptimes={}),
        health_checker=SubprocessHealthChecker(),
    )

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "unhealthy"


def test_status_env_document_uptime_health_stopped_pane_is_unhealthy_without_probing() -> None:
    """A stopped pane is unhealthy without ever consulting child_uptime_seconds."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    ctx = _make_ctx(manifest=_uptime_manifest("30s"))
    svc = _make_service(
        tmux=tmux,
        reaper=FakeProcessReaper(children_set=set(), child_uptimes={10: 300}),
        health_checker=SubprocessHealthChecker(),
    )

    doc = svc.status_env_document(ctx)

    assert doc["services"][0]["health"] == "unhealthy"


def test_restart_resets_uptime_clock() -> None:
    """restart() reaps the pane's child and relaunches; the uptime probe reads the
    reaper live on every call, so a freshly-measured (low) child uptime after
    restart flips a previously-healthy service back to unhealthy — the clock
    is naturally reset because it is anchored on the (new) child process.
    """
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    manifest = _uptime_manifest("30s")
    ctx = _make_ctx(manifest=manifest, inject_scope="alpha")
    reaper = FakeProcessReaper(children_set={10}, child_uptimes={10: 300})
    svc = _make_service(tmux=tmux, reaper=reaper, health_checker=SubprocessHealthChecker())

    before = svc.status_env_document(ctx)
    assert before["services"][0]["health"] == "healthy"

    result = svc.restart(ctx, "backend")
    assert result == 0

    # Simulate the reaper now measuring the freshly relaunched child's low
    # elapsed time (the real PgrepProcessReaper would naturally observe this
    # since restart() reaps the old child and a brand-new one gets forked).
    reaper._child_uptimes[10] = 1

    after = svc.status_env_document(ctx)
    assert after["services"][0]["health"] == "unhealthy"


def test_status_env_document_port_base_from_env_vars() -> None:
    """port_base is parsed from WINTER_PORT_BASE in the env vars; None when absent."""
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    doc_with = svc.status_env_document(_make_ctx(env_vars={"WINTER_PORT_BASE": "4020"}))
    assert doc_with["port_base"] == 4020

    doc_without = svc.status_env_document(_make_ctx())
    assert doc_without["port_base"] is None


def test_status_env_document_workspace_port_base_from_workspace_var() -> None:
    """workspace scope reads WINTER_WORKSPACE_PORT_BASE; the per-env WINTER_PORT_BASE is ignored."""
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    # Workspace band present (plus a stray per-env var that must be ignored).
    ws_ctx = dataclasses.replace(
        _make_ctx(env_vars={"WINTER_WORKSPACE_PORT_BASE": "4000", "WINTER_PORT_BASE": "4020"}),
        env="workspace",
    )
    assert svc.status_env_document(ws_ctx)["port_base"] == 4000

    # No workspace band -> None, even with a per-env WINTER_PORT_BASE in scope.
    ws_ctx_absent = dataclasses.replace(
        _make_ctx(env_vars={"WINTER_PORT_BASE": "4020"}),
        env="workspace",
    )
    assert svc.status_env_document(ws_ctx_absent)["port_base"] is None


def test_status_env_document_services_filter() -> None:
    """The services filter narrows the document to the requested names."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(children_set={10})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    doc = svc.status_env_document(ctx, services=("backend",))

    assert [s["name"] for s in doc["services"]] == ["backend"]


def test_status_env_document_writes_nothing_to_stdout() -> None:
    """status_env_document is pure of stdout — the entrypoint serialises."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=out)

    svc.status_env_document(ctx)

    assert out.getvalue() == ""


# ---------------------------------------------------------------------------
# Output seam — user-facing lines go through injected stdout/stderr, not print()
# ---------------------------------------------------------------------------


def test_up_started_message_goes_through_stdout_seam() -> None:
    """up() writes its 'Started services' message to the injected stdout seam."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, hook_runner=hook, stdout=out)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    result = svc.up(ctx)

    assert result == 0
    assert "Started services" in out.getvalue()


def test_down_stopped_message_goes_through_stdout_seam() -> None:
    """down() writes its 'Stopped services' message to the injected stdout seam."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    ctx = _make_ctx()
    out = io.StringIO()
    svc = _make_service(tmux=tmux, stdout=out)

    result = svc.down(ctx)

    assert result == 0
    assert "Stopped services" in out.getvalue()


def test_restart_message_goes_through_stdout_seam() -> None:
    """restart() writes its 'Restarted' message to the injected stdout seam."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    ctx = _make_ctx(inject_scope="alpha")
    out = io.StringIO()
    svc = _make_service(tmux=tmux, stdout=out)

    result = svc.restart(ctx, "backend")

    assert result == 0
    assert "Restarted" in out.getvalue()
    assert "backend" in out.getvalue()


def test_prune_failure_warning_goes_through_stderr_seam() -> None:
    """Prune failure warning goes through the injected stderr seam, not print()."""

    class _BrokenLogRepo(FakeLogRepository):
        def rotated_segments(self, worktree_dir: Path, service: str) -> list[Path]:
            raise RuntimeError("disk error simulated")

    tmux = FakeTmuxRepository()
    log_repo = _BrokenLogRepo()
    clock = FakeFollowClock(current_time=10000.0)
    hook = FakeLayoutHookRunner()
    err = io.StringIO()

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo, clock=clock, stderr=err)
    result = svc.up(ctx)

    assert result == 0
    assert "prune failed" in err.getvalue()


# ---------------------------------------------------------------------------
# Startup retry — _await_startup and up(retry=True/False)
# ---------------------------------------------------------------------------

_MANIFEST_WITH_STARTUP_TOML = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.startup]
retries = 2
retry_delay = 1.0
"""


def _make_startup_ctx(toml: str = _MANIFEST_WITH_STARTUP_TOML) -> SessionContext:
    """Build a ctx from a manifest with a startup policy on backend."""
    manifest = _make_manifest(toml)
    return _make_ctx(manifest=manifest, inject_scope=None)


def _up_with_panes(
    toml: str,
    pane_map: dict[str, int],
    *,
    reaper: FakeProcessReaper,
    clock: FakeFollowClock,
    retry: bool,
) -> tuple[int, FakeTmuxRepository, io.StringIO, io.StringIO]:
    """Run up(retry=*retry*) with the given pane map pre-seeded after new_session.

    Manifests used here have no layout_hook, so real tmux would create the
    default pane (0.0) during new_session.  We mimic that by patching
    new_session to also seed the pane map.
    """
    tmux = FakeTmuxRepository()
    out = io.StringIO()
    err = io.StringIO()

    _orig_new_session = tmux.new_session

    def _new_session_with_panes(session: str, cwd: object, width: object, height: object) -> None:
        _orig_new_session(session, cwd, width, height)  # type: ignore[arg-type]
        tmux._sessions[session].update(pane_map)

    tmux.new_session = _new_session_with_panes  # type: ignore[method-assign]

    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)
    svc = _make_service(tmux=tmux, reaper=reaper, clock=clock, stdout=out, stderr=err)
    rc = svc.up(ctx, retry=retry)
    return rc, tmux, out, err


def test_up_retry_true_survives_settle_window_no_resend() -> None:
    """retry=True, service alive after settle → no extra send_keys, rc 0."""
    # backend pid=100 is alive on first has_children call
    reaper = FakeProcessReaper(children_set={100})
    clock = FakeFollowClock()

    rc, tmux, _out, err = _up_with_panes(
        _MANIFEST_WITH_STARTUP_TOML,
        {"0.0": 100},
        reaper=reaper,
        clock=clock,
        retry=True,
    )

    assert rc == 0
    # Exactly one send_keys for the initial launch, no extra re-sends
    assert len(tmux.sent) == 1
    # Settle sleep recorded
    assert clock.sleep_calls == [0.5]
    assert err.getvalue() == ""


def test_up_retry_true_dead_then_alive_after_one_retry() -> None:
    """retry=True, dead on settle then alive after 1 retry → one extra send_keys, rc 0."""
    # First has_children call → False (dead), second → True (alive)
    reaper = FakeProcessReaper(children_sequence={100: [False, True]})
    clock = FakeFollowClock()

    rc, tmux, _out, err = _up_with_panes(
        _MANIFEST_WITH_STARTUP_TOML,
        {"0.0": 100},
        reaper=reaper,
        clock=clock,
        retry=True,
    )

    assert rc == 0
    # Initial send + one retry send
    assert len(tmux.sent) == 2
    # Settle sleep + one retry_delay sleep (1.0)
    assert clock.sleep_calls == [0.5, 1.0]
    assert err.getvalue() == ""


def test_up_retry_true_exhausted_retries_returns_rc1_with_failed_name() -> None:
    """retry=True, always dead → rc 1, service named in stderr."""
    # All has_children calls return False (dead) — initial check + 2 retries
    reaper = FakeProcessReaper(children_sequence={100: [False, False, False]})
    clock = FakeFollowClock()
    err = io.StringIO()

    toml = _MANIFEST_WITH_STARTUP_TOML  # retries=2
    rc, tmux, _out, err = _up_with_panes(
        toml,
        {"0.0": 100},
        reaper=reaper,
        clock=clock,
        retry=True,
    )

    assert rc == 1
    # Initial send + 2 retry sends
    assert len(tmux.sent) == 3
    # Settle sleep + 2 retry_delay sleeps (1.0 each)
    assert clock.sleep_calls == [0.5, 1.0, 1.0]
    assert "backend" in err.getvalue()
    assert "Services failed to stay up after retries" in err.getvalue()


def test_up_retry_true_no_policy_not_monitored() -> None:
    """retry=True but service has no startup policy → not monitored, no settle sleep."""
    toml = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
"""
    # No startup policy on backend — reaper never consulted for retry
    reaper = FakeProcessReaper()
    clock = FakeFollowClock()

    rc, tmux, _out, err = _up_with_panes(
        toml,
        {"0.0": 100},
        reaper=reaper,
        clock=clock,
        retry=True,
    )

    assert rc == 0
    assert len(tmux.sent) == 1
    # No settle sleep because no candidates
    assert clock.sleep_calls == []
    assert err.getvalue() == ""


def test_up_retry_false_with_policy_no_settle_no_extra_sends() -> None:
    """retry=False (default) with a startup policy → zero sleeps, zero extra sends."""
    # If reaper.has_children is called by mistake it returns False → would cause rc=1
    reaper = FakeProcessReaper(children_sequence={100: [False, False, False]})
    clock = FakeFollowClock()

    rc, tmux, _out, err = _up_with_panes(
        _MANIFEST_WITH_STARTUP_TOML,
        {"0.0": 100},
        reaper=reaper,
        clock=clock,
        retry=False,
    )

    assert rc == 0
    # Only the initial send_keys, no retries
    assert len(tmux.sent) == 1
    # No sleeps at all
    assert clock.sleep_calls == []
    assert err.getvalue() == ""


def test_up_retry_true_settle_and_retry_delay_go_through_clock_seam() -> None:
    """Verify sleep calls use clock seam with exact values: settle + N*retry_delay."""
    toml = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.0"
cmd = "run"

[service.startup]
retries = 3
retry_delay = 0.25
"""
    # Dead on settle, then alive on first retry
    reaper = FakeProcessReaper(children_sequence={42: [False, True]})
    clock = FakeFollowClock()

    rc, _tmux, _out, _err = _up_with_panes(
        toml,
        {"0.0": 42},
        reaper=reaper,
        clock=clock,
        retry=True,
    )

    assert rc == 0
    # Settle (0.5) + one retry_delay (0.25)
    assert clock.sleep_calls == [0.5, 0.25]


def test_up_retry_true_reaps_descendants_before_relaunch() -> None:
    """A dead candidate's pane descendants are reaped before each re-launch.

    has_children sees only direct children; a lingering grandchild would stack a
    second process into the pane. The retry loop reaps descendants first, mirroring
    restart/down.
    """
    # Dead on settle, alive after one retry; pane 100 has an orphaned descendant 555.
    reaper = FakeProcessReaper(
        descendant_map={100: [555]},
        children_sequence={100: [False, True]},
    )
    clock = FakeFollowClock()

    rc, tmux, _out, _err = _up_with_panes(
        _MANIFEST_WITH_STARTUP_TOML,
        {"0.0": 100},
        reaper=reaper,
        clock=clock,
        retry=True,
    )

    assert rc == 0
    # The retry reaped pane 100's descendants ([555]) before re-sending.
    assert reaper.killed == [[555]]
    # Initial send + one retry send.
    assert len(tmux.sent) == 2


def test_up_retry_true_emits_per_attempt_progress() -> None:
    """Each re-launch attempt writes a numbered progress line through the stdout seam."""
    reaper = FakeProcessReaper(children_sequence={100: [False, False, True]})
    clock = FakeFollowClock()

    rc, _tmux, out, _err = _up_with_panes(
        _MANIFEST_WITH_STARTUP_TOML,  # retries=2
        {"0.0": 100},
        reaper=reaper,
        clock=clock,
        retry=True,
    )

    assert rc == 0
    output = out.getvalue()
    assert "Retrying 'backend' (attempt 1/2)" in output
    assert "Retrying 'backend' (attempt 2/2)" in output


# ---------------------------------------------------------------------------
# depends_on — _topological_order, _dependency_ready (pure functions)
# ---------------------------------------------------------------------------


def _svc(name: str, window: int, pane: int, depends_on: tuple[str, ...] = ()) -> Service:
    return Service(name=name, target=Target(window=window, pane=pane), cmd=f"run {name}", depends_on=depends_on)


def test_topological_order_reorders_dependent_after_dependency() -> None:
    """A dependent declared BEFORE its dependency is reordered to launch after it."""
    api = _svc("api", 0, 0, depends_on=("builder",))
    builder = _svc("builder", 0, 1)

    ordered = _topological_order((api, builder), "alpha")

    assert [s.name for s in ordered] == ["builder", "api"]


def test_topological_order_no_deps_preserves_declaration_order() -> None:
    backend = _svc("backend", 0, 0)
    frontend = _svc("frontend", 0, 1)

    ordered = _topological_order((backend, frontend), "alpha")

    assert [s.name for s in ordered] == ["backend", "frontend"]


def test_topological_order_cross_provider_pattern_does_not_reorder() -> None:
    """A '/'-qualified depends_on entry for a DIFFERENT scope is not a local edge — order is unaffected."""
    api = _svc("api", 0, 0, depends_on=("workspace/db",))
    other = _svc("other", 0, 1)

    ordered = _topological_order((api, other), "alpha")

    assert [s.name for s in ordered] == ["api", "other"]


def test_topological_order_qualified_same_scope_dep_is_ordered() -> None:
    """A depends_on entry qualified with THIS SAME scope (e.g. 'alpha/builder' declared inside
    env 'alpha') resolves to the identical poll target as the bare form and must be sequenced
    identically — regression test for the deadlock where only the bare spelling was locally
    ordered, leaving the dependency launched AFTER its dependent (never, since the loop blocks
    synchronously on each service's depends_on gate before advancing)."""
    api = _svc("api", 0, 0, depends_on=("alpha/builder",))
    builder = _svc("builder", 0, 1)

    ordered = _topological_order((api, builder), "alpha")

    assert [s.name for s in ordered] == ["builder", "api"]


def test_topological_order_chain_of_three() -> None:
    api = _svc("api", 0, 0, depends_on=("worker",))
    worker = _svc("worker", 0, 1, depends_on=("builder",))
    builder = _svc("builder", 0, 2)

    ordered = _topological_order((api, worker, builder), "alpha")

    assert [s.name for s in ordered] == ["builder", "worker", "api"]


def test_topological_order_defensive_against_runtime_cycle() -> None:
    """A cycle (should be caught by the validator) does not hang — all names still returned."""
    a = _svc("a", 0, 0, depends_on=("b",))
    b = _svc("b", 0, 1, depends_on=("a",))

    ordered = _topological_order((a, b), "alpha")

    assert {s.name for s in ordered} == {"a", "b"}
    assert len(ordered) == 2


def test_dependency_ready_healthy_is_ready() -> None:
    assert _dependency_ready(DependencyStatus(state="running", health="healthy")) is True


def test_dependency_ready_unknown_health_running_state_is_ready() -> None:
    """Liveness fallback: no declared probe (health=unknown) but the process is running."""
    assert _dependency_ready(DependencyStatus(state="running", health="unknown")) is True


def test_dependency_ready_unhealthy_is_not_ready() -> None:
    assert _dependency_ready(DependencyStatus(state="running", health="unhealthy")) is False


def test_dependency_ready_unknown_health_stopped_state_is_not_ready() -> None:
    assert _dependency_ready(DependencyStatus(state="stopped", health="unknown")) is False


def test_dependency_ready_none_is_not_ready() -> None:
    assert _dependency_ready(None) is False


# ---------------------------------------------------------------------------
# depends_on — up() integration: ordering, gating, cross-provider, timeout
# ---------------------------------------------------------------------------

_MANIFEST_WITH_DEPENDS_ON_TOML = """\
session_prefix = "mp"
layout_hook = "layout-hook.sh"

[[service]]
name = "api"
target = "0.0"
cmd = "run api"
depends_on = ["builder"]

[[service]]
name = "builder"
target = "0.1"
cmd = "run builder"
"""


def _make_depends_on_ctx() -> SessionContext:
    manifest = _make_manifest(_MANIFEST_WITH_DEPENDS_ON_TOML)
    return _make_ctx(manifest=manifest, inject_scope=None)


def test_up_launches_in_topological_order_and_gates_on_dependency() -> None:
    """api (declared first) depends on builder (declared second) — builder launches first."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock()
    winter_cli = FakeWinterCli(statuses={"alpha/builder": DependencyStatus(state="running", health="healthy")})

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_depends_on_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli)

    result = svc.up(ctx)

    assert result == 0
    # builder (0.1) launches before api (0.0) despite declaration order.
    assert [target for _, target, _ in tmux.sent] == ["0.1", "0.0"]
    # Only api declares a dependency; builder's own launch triggers no poll.
    assert winter_cli.status_calls == ["alpha/builder"]


def test_up_qualified_same_scope_dependency_is_ordered_and_launches() -> None:
    """A depends_on entry qualified with the SAME env ("alpha/builder" declared inside env
    "alpha") must be locally sequenced exactly like the bare "builder" spelling — regression
    test for the deadlock where only bare deps were reordered, so a qualified same-scope
    dependent was reached before its dependency and (with a real winter_cli seam) would poll
    forever waiting for a service that never got its own send_keys."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock()
    winter_cli = FakeWinterCli(statuses={"alpha/builder": DependencyStatus(state="running", health="healthy")})

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    toml = """\
session_prefix = "mp"
layout_hook = "layout-hook.sh"

[[service]]
name = "api"
target = "0.0"
cmd = "run api"
depends_on = ["alpha/builder"]

[[service]]
name = "builder"
target = "0.1"
cmd = "run builder"
"""
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli)

    result = svc.up(ctx)

    assert result == 0
    # builder (0.1) launches before api (0.0) — the qualified same-scope
    # dependency is sequenced identically to a bare one.
    assert [target for _, target, _ in tmux.sent] == ["0.1", "0.0"]
    assert winter_cli.status_calls == ["alpha/builder"]
    # No timeout: the clock never had to advance past the poll (ready first try).
    assert clock.sleep_calls == []


def test_up_gate_polls_until_dependency_becomes_ready() -> None:
    """The gate polls (sleeping between ticks) until health flips from unhealthy to healthy."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock()
    winter_cli = FakeWinterCli(
        statuses={
            "alpha/builder": [
                DependencyStatus(state="running", health="unhealthy"),
                DependencyStatus(state="running", health="unhealthy"),
                DependencyStatus(state="running", health="healthy"),
            ]
        }
    )

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_depends_on_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli)

    result = svc.up(ctx)

    assert result == 0
    assert len(winter_cli.status_calls) == 3
    assert clock.sleep_calls == [1.0, 1.0]
    assert [target for _, target, _ in tmux.sent] == ["0.1", "0.0"]


def test_up_cross_provider_dependency_via_faked_winter_cli_seam() -> None:
    """depends_on = ["workspace/db"] resolves through the SAME seam as a same-env dependency."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock()
    winter_cli = FakeWinterCli(statuses={"workspace/db": DependencyStatus(state="running", health="healthy")})

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100})

    hook._side_effect = _add_panes

    toml = """\
session_prefix = "mp"
layout_hook = "layout-hook.sh"

[[service]]
name = "api"
target = "0.0"
cmd = "run api"
depends_on = ["workspace/db"]
"""
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, inject_scope=None)
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli)

    result = svc.up(ctx)

    assert result == 0
    assert winter_cli.status_calls == ["workspace/db"]
    assert len(tmux.sent) == 1


def test_up_dependency_timeout_exits_nonzero_and_names_dependent_and_dependency() -> None:
    """A dependency that never becomes ready before the timeout fails up() with both names."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock(current_time=0.0)
    err = io.StringIO()
    # Always unhealthy — never satisfies the gate.
    winter_cli = FakeWinterCli(statuses={"alpha/builder": DependencyStatus(state="running", health="unhealthy")})

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_depends_on_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli, stderr=err)

    result = svc.up(ctx)

    assert result == 1
    # builder has no dependency of its own and launches; api never does — its
    # gate on 'alpha/builder' times out before api's send_keys.
    assert [target for _, target, _ in tmux.sent] == ["0.1"]
    message = err.getvalue()
    assert "api" in message
    assert "alpha/builder" in message
    # Clock advanced to (at least) the 120s default timeout via repeated 1s polls.
    assert clock.current_time >= 120.0


def test_up_dependency_timeout_honors_injected_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """WINTER_SERVICE_TIMEOUT=6 shortens the gate's deadline from the 120s default to ~6s."""
    monkeypatch.setenv("WINTER_SERVICE_TIMEOUT", "6")
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock(current_time=0.0)
    err = io.StringIO()
    # Always unhealthy — never satisfies the gate.
    winter_cli = FakeWinterCli(statuses={"alpha/builder": DependencyStatus(state="running", health="unhealthy")})

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_depends_on_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli, stderr=err)

    result = svc.up(ctx)

    assert result == 1
    # Clock advanced to (at least) the injected 6s ceiling, not the 120s default.
    assert 6.0 <= clock.current_time < 120.0


def test_depends_on_timeout_seconds_defaults_to_120_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINTER_SERVICE_TIMEOUT", raising=False)
    assert _depends_on_timeout_seconds() == 120.0


@pytest.mark.parametrize("raw", ["", "not-a-number", "0", "-5"])
def test_depends_on_timeout_seconds_falls_back_to_120_on_invalid_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("WINTER_SERVICE_TIMEOUT", raw)
    assert _depends_on_timeout_seconds() == 120.0


def test_depends_on_timeout_seconds_parses_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTER_SERVICE_TIMEOUT", "6")
    assert _depends_on_timeout_seconds() == 6.0


def test_up_dependency_winter_cli_unavailable_fails_fast_without_exhausting_timeout() -> None:
    """A missing/misconfigured 'winter' CLI fails immediately rather than polling out 120s."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock(current_time=0.0)
    err = io.StringIO()
    winter_cli = FakeWinterCli(statuses={"alpha/builder": WinterCliUnavailableError("'winter' not found on PATH")})

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_depends_on_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli, stderr=err)

    result = svc.up(ctx)

    assert result == 1
    # builder has no dependency of its own and launches; api's gate raises
    # immediately on the first poll — no send_keys, no 120s of polling.
    assert [target for _, target, _ in tmux.sent] == ["0.1"]
    assert clock.current_time == 0.0
    message = err.getvalue()
    assert "api" in message
    assert "not found on PATH" in message


def test_up_no_depends_on_never_touches_winter_cli_seam() -> None:
    """A manifest with no depends_on declarations never calls the winter_cli seam."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock()
    winter_cli = FakeWinterCli()
    ctx = _make_ctx()

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=winter_cli)
    result = svc.up(ctx)

    assert result == 0
    assert winter_cli.status_calls == []


def test_up_depends_on_without_winter_cli_seam_skips_gate() -> None:
    """No winter_cli injected (None) → depends_on is a no-op, matching _await_startup's seam-optional fallback."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    clock = FakeFollowClock()

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    ctx = _make_depends_on_ctx()
    svc = _make_service(tmux=tmux, hook_runner=hook, clock=clock, winter_cli=None)

    result = svc.up(ctx)

    assert result == 0
    assert len(tmux.sent) == 2


# ---------------------------------------------------------------------------
# _resolve_service_port — pure function unit tests
# ---------------------------------------------------------------------------


def test_resolve_service_port_none_returns_none() -> None:
    assert _resolve_service_port(None, 4060) is None


def test_resolve_service_port_none_without_port_base_returns_none() -> None:
    assert _resolve_service_port(None, None) is None


def test_resolve_service_port_literal_int_returned_as_is() -> None:
    assert _resolve_service_port(4070, None) == 4070


def test_resolve_service_port_literal_int_ignores_port_base() -> None:
    assert _resolve_service_port(4070, 9000) == 4070


def test_resolve_service_port_offset_expression_resolved() -> None:
    assert _resolve_service_port("WINTER_PORT_BASE + 10", 4060) == 4070


def test_resolve_service_port_offset_expression_resolved_different_base() -> None:
    assert _resolve_service_port("WINTER_PORT_BASE + 11", 4060) == 4071


def test_resolve_service_port_offset_expression_without_port_base_returns_none() -> None:
    assert _resolve_service_port("WINTER_PORT_BASE + 10", None) is None


def test_resolve_service_port_offset_zero_with_base() -> None:
    assert _resolve_service_port("WINTER_PORT_BASE + 0", 4060) == 4060


def test_resolve_service_port_offset_expression_with_whitespace() -> None:
    assert _resolve_service_port("  WINTER_PORT_BASE  +  5  ", 4060) == 4065


def test_resolve_service_port_invalid_expression_returns_none() -> None:
    assert _resolve_service_port("PORT_BASE + 10", 4060) is None


# ---------------------------------------------------------------------------
# status_env_document — port resolution end-to-end
# ---------------------------------------------------------------------------


def test_status_env_document_literal_port_in_ports_column() -> None:
    """A literal port value appears in the service's ports list."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    reaper = FakeProcessReaper(children_set={10})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(Service(name="web", target=Target(0, 0), cmd="cmd", port=4070),),
    )
    ctx = _make_ctx(manifest=manifest)
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    doc = svc.status_env_document(ctx)

    web = doc["services"][0]
    assert web["ports"] == [4070]


def test_status_env_document_offset_expression_resolved_against_port_base() -> None:
    """An offset expression resolves to port_base + offset for the env."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(children_set={10, 20})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(
            Service(name="web", target=Target(0, 0), cmd="cmd", port="WINTER_PORT_BASE + 10"),
            Service(name="api", target=Target(0, 1), cmd="cmd", port="WINTER_PORT_BASE + 11"),
        ),
    )
    ctx = _make_ctx(manifest=manifest, env_vars={"WINTER_PORT_BASE": "4060"})
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    doc = svc.status_env_document(ctx)

    by_name = {s["name"]: s for s in doc["services"]}
    assert by_name["web"]["ports"] == [4070]  # 4060 + 10
    assert by_name["api"]["ports"] == [4071]  # 4060 + 11


def test_status_env_document_no_port_renders_blank() -> None:
    """A service without a port field renders ports as [] (blank)."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    reaper = FakeProcessReaper(children_set={10})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(Service(name="worker", target=Target(0, 0), cmd="cmd"),),
    )
    ctx = _make_ctx(manifest=manifest, env_vars={"WINTER_PORT_BASE": "4060"})
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    doc = svc.status_env_document(ctx)

    worker = doc["services"][0]
    assert worker["ports"] == []


def test_status_env_document_offset_expression_without_port_base_renders_blank() -> None:
    """When WINTER_PORT_BASE is absent, an offset-expression port renders blank."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10})
    reaper = FakeProcessReaper(children_set={10})
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook=None,
        services=(Service(name="web", target=Target(0, 0), cmd="cmd", port="WINTER_PORT_BASE + 10"),),
    )
    # No WINTER_PORT_BASE in env_vars
    ctx = _make_ctx(manifest=manifest, env_vars={})
    svc = _make_service(tmux=tmux, reaper=reaper, stdout=io.StringIO())

    doc = svc.status_env_document(ctx)

    web = doc["services"][0]
    assert web["ports"] == []
