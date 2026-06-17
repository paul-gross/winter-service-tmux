"""Tests for OrchestratorService — full action matrix against hand-rolled fakes.

All four actions are tested: up, down, status, restart.  No subprocess calls
are made; all I/O is mediated through the fakes defined in tests/conftest.py.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

import service_manifest.container as sm_container_mod
from service_manifest.modules.manifest.model import LogConfig, Service, ServiceManifest, Target
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.orchestrator_service import OrchestratorService, _segments_to_prune
from service_orchestrator.modules.orchestrate.status_report import logwriter_path
from tests.conftest import (
    FakeFollowClock,
    FakeLayoutHookRunner,
    FakeLogRepository,
    FakeProcessReaper,
    FakeTmuxRepository,
)
from tests.fakes import FakeFilesystemReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_WORKTREE = _WORKSPACE / "alpha"
_ENV_FILE = _WORKTREE / ".winter.env"

# A two-service manifest (backend 0.0, frontend 0.1) with a layout hook.
_MANIFEST_TOML = """\
session_prefix = "mp"
env_file = ".winter.env"
layout_hook = "ai/project/layout-hook.sh"

[[service]]
name = "backend"
target = "0.0"
command = "npm run start:dev"

[[service]]
name = "frontend"
target = "0.1"
command = "npm run dev"

[[status.url]]
label = "Backend"
url = "http://localhost:${BACKEND_PORT}"
"""

_MANIFEST_COMMITTED_PATH = Path("ai/project/setup-tmux.toml")
_ENV_FILE_REL = Path(".winter.env")


def _make_sm_container(toml_content: str = _MANIFEST_TOML) -> sm_container_mod.Container:
    """Build a service_manifest.Container seeded with an in-memory TOML."""
    abs_toml_path = _WORKSPACE / _MANIFEST_COMMITTED_PATH
    fake_fs = FakeFilesystemReader({abs_toml_path: toml_content})
    return sm_container_mod.Container(fs=fake_fs)


def _make_manifest(toml_content: str = _MANIFEST_TOML) -> ServiceManifest:
    sm = _make_sm_container(toml_content)
    return sm.manifest_reader.read(_WORKSPACE)


def _make_ctx(
    manifest: ServiceManifest | None = None,
    env_vars: dict[str, str] | None = None,
    env_file_path: Path | None = _ENV_FILE,
) -> EnvContext:
    if manifest is None:
        manifest = _make_manifest()
    return EnvContext(
        env="alpha",
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKTREE,
        manifest=manifest,
        env_vars=env_vars,
        env_file_path=env_file_path,
    )


def _make_service(
    tmux: FakeTmuxRepository | None = None,
    reaper: FakeProcessReaper | None = None,
    hook_runner: FakeLayoutHookRunner | None = None,
    log_repo: FakeLogRepository | None = None,
    clock: FakeFollowClock | None = None,
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
        clock=clock,
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
    assert hook_path == _WORKSPACE / "ai/project/layout-hook.sh"
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


def test_up_send_keys_exact_launch_line_with_env_file() -> None:
    """Captured services get the writer-wrapped launch line; log=False gets bare."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()
    ctx = _make_ctx(env_file_path=_ENV_FILE)
    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    # Both services are captured (log=LogMode.FILE default, non-empty command).
    writer = logwriter_path()
    manifest = ctx.manifest
    rotate_size = manifest.logs.rotate_size_bytes
    max_rot = manifest.logs.max_rotations

    # backend line — captured
    backend_line = next(line for _, t, line in tmux.sent if t == "0.0")
    backend_logfile = log_repo.log_path(_WORKTREE, "backend")
    expected_backend = (
        f"cd {shlex.quote(str(_WORKTREE))} && source {shlex.quote(str(_ENV_FILE))}"
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
        f"cd {shlex.quote(str(_WORKTREE))} && source {shlex.quote(str(_ENV_FILE))}"
        f" && echo {shlex.quote('=== frontend ===')} && "
        f"{{ npm run dev ; }} 2>&1 | "
        f"python3 {shlex.quote(str(writer))} {shlex.quote(str(frontend_logfile))} "
        f"--rotate-size {rotate_size} --max-rotations {max_rot}"
    )
    assert frontend_line == expected_frontend

    # ensure_log_dir was called once
    assert log_repo.ensure_log_dir_calls == [_WORKTREE]


def test_up_send_keys_exact_launch_line_without_env_file() -> None:
    """Without env_file_path, the source step is omitted; captured service still wrapped."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()
    ctx = _make_ctx(env_file_path=None)
    svc = _make_service(tmux=tmux, hook_runner=hook, log_repo=log_repo)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    writer = logwriter_path()
    manifest = ctx.manifest
    rotate_size = manifest.logs.rotate_size_bytes
    max_rot = manifest.logs.max_rotations
    backend_logfile = log_repo.log_path(_WORKTREE, "backend")

    backend_line = next(line for _, t, line in tmux.sent if t == "0.0")
    expected = (
        f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== backend ===')} && "
        f"{{ npm run start:dev ; }} 2>&1 | "
        f"python3 {shlex.quote(str(writer))} {shlex.quote(str(backend_logfile))} "
        f"--rotate-size {rotate_size} --max-rotations {max_rot}"
    )
    assert backend_line == expected


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
command = "npm run start:dev"
"""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, env_file_path=None)
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


def test_up_idempotent_when_session_exists(capsys: pytest.CaptureFixture[str]) -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 100})
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux)

    result = svc.up(ctx)

    assert result == 0
    assert tmux.sent == []
    captured = capsys.readouterr()
    assert "already running" in captured.out


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


def test_up_salvage_hook_fail_multiple_windows_keeps_session(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    svc = _make_service(tmux=tmux, hook_runner=hook)

    result = svc.up(ctx)

    assert result == 1
    assert "mp-alpha" in tmux._sessions
    assert "mp-alpha" not in tmux.killed_sessions
    captured = capsys.readouterr()
    assert "Warning" in captured.out


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


def test_down_noop_when_no_session(capsys: pytest.CaptureFixture[str]) -> None:
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.down(ctx)

    assert result == 0
    assert reaper.killed == []
    captured = capsys.readouterr()
    assert "No running session" in captured.out


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
# status — running / stopped / missing + status URL header
# ---------------------------------------------------------------------------


def test_status_running_service(capsys: pytest.CaptureFixture[str]) -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    tmux.capture_text = {"0.0": "Starting server...\nListening on :3000\n"}
    reaper = FakeProcessReaper(children_set={10})  # 10 has children → running
    ctx = _make_ctx(env_vars={"BACKEND_PORT": "3000"})
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.status(ctx)

    assert result == 0
    captured = capsys.readouterr()
    assert "backend:" in captured.out
    assert "running" in captured.out
    assert "Listening on :3000" in captured.out


def test_status_stopped_service(capsys: pytest.CaptureFixture[str]) -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(children_set=set())  # no children → stopped
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.status(ctx)

    assert result == 0
    captured = capsys.readouterr()
    assert "stopped" in captured.out


def test_status_missing_pane(capsys: pytest.CaptureFixture[str]) -> None:
    tmux = FakeTmuxRepository()
    # Only pane 0.0 exists; manifest also declares 0.1
    tmux.seed_session("mp-alpha", {"0.0": 10})
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.status(ctx)

    assert result == 0
    captured = capsys.readouterr()
    assert "missing" in captured.out
    assert "frontend" in captured.out


def test_status_interpolates_status_url_header(capsys: pytest.CaptureFixture[str]) -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper()
    ctx = _make_ctx(env_vars={"BACKEND_PORT": "4100"})
    svc = _make_service(tmux=tmux, reaper=reaper)

    svc.status(ctx)

    captured = capsys.readouterr()
    assert "http://localhost:4100" in captured.out


def test_status_unresolved_var_left_literal(capsys: pytest.CaptureFixture[str]) -> None:
    """When env_vars is None, ${VAR} placeholders are left as literals."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper()
    ctx = _make_ctx(env_vars=None)
    svc = _make_service(tmux=tmux, reaper=reaper)

    svc.status(ctx)

    captured = capsys.readouterr()
    assert "${BACKEND_PORT}" in captured.out


def test_status_no_session(capsys: pytest.CaptureFixture[str]) -> None:
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    ctx = _make_ctx()
    svc = _make_service(tmux=tmux, reaper=reaper)

    result = svc.status(ctx)

    assert result == 0
    captured = capsys.readouterr()
    assert "No mp-alpha session running" in captured.out


# ---------------------------------------------------------------------------
# restart — reaps target pane children + re-sends
# ---------------------------------------------------------------------------


def test_restart_reaps_children_and_resends() -> None:
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-alpha", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(descendant_map={10: [100, 101]})
    ctx = _make_ctx(env_file_path=_ENV_FILE)
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
        f"cd {shlex.quote(str(_WORKTREE))} && source {shlex.quote(str(_ENV_FILE))}"
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
command = "cmd"
"""
    manifest = _make_manifest(toml)
    ctx2 = _make_ctx(manifest=manifest, env_file_path=None)
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
command = ""
"""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, env_file_path=None)

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
command = "npm run docs"

[logs]
rotate_size_bytes = 5242880
max_rotations = 3
"""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, env_file_path=None)

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
command = "python -m worker"
log = "pane"
"""
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, env_file_path=None)

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
command = "python -m worker"
log = "memory"
"""
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, env_file_path=None)

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
command = "python -m api"
log = "file"
"""
    tmux = FakeTmuxRepository()
    log_repo = FakeLogRepository()
    manifest = _make_manifest(toml)
    ctx = _make_ctx(manifest=manifest, env_file_path=None)

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
        services=(Service(name="docs", target=Target(window=0, pane=0), command="cmd"),),
        status_urls=(),
        logs=LogConfig(retention_seconds=retention_seconds),
    )


def _make_prune_ctx(retention_seconds: int = 604800) -> EnvContext:
    return EnvContext(
        env="alpha",
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKTREE,
        manifest=_make_prune_manifest(retention_seconds),
        env_vars=None,
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
    svc = OrchestratorService(tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock)
    svc.prune(ctx)

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
    svc = OrchestratorService(tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock)
    svc.prune(ctx)

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
    svc = OrchestratorService(tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock)
    svc.prune(ctx)

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
    svc = OrchestratorService(tmux=tmux, reaper=FakeProcessReaper(), hook_runner=FakeLayoutHookRunner(), log_repo=log_repo, clock=clock)
    svc.prune(ctx)

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
