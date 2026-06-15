"""Tests for OrchestratorService — full action matrix against hand-rolled fakes.

All four actions are tested: up, down, status, restart.  No subprocess calls
are made; all I/O is mediated through the fakes defined in tests/conftest.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import service_manifest.container as sm_container_mod
from service_manifest.modules.manifest.model import ServiceManifest
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.orchestrator_service import OrchestratorService
from tests.conftest import FakeLayoutHookRunner, FakeProcessReaper, FakeTmuxRepository
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
) -> OrchestratorService:
    if tmux is None:
        tmux = FakeTmuxRepository()
    if reaper is None:
        reaper = FakeProcessReaper()
    if hook_runner is None:
        hook_runner = FakeLayoutHookRunner()
    return OrchestratorService(
        tmux=tmux,
        reaper=reaper,
        hook_runner=hook_runner,
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
    """The exact launch line matches the bash winter_tmux_send_service pattern."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx(env_file_path=_ENV_FILE)
    svc = _make_service(tmux=tmux, hook_runner=hook)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    # backend line
    backend_line = next(line for _, t, line in tmux.sent if t == "0.0")
    expected_backend = f"cd '{_WORKTREE}' && source '{_ENV_FILE}' && echo '=== backend ===' && npm run start:dev"
    assert backend_line == expected_backend

    # frontend line
    frontend_line = next(line for _, t, line in tmux.sent if t == "0.1")
    expected_frontend = f"cd '{_WORKTREE}' && source '{_ENV_FILE}' && echo '=== frontend ===' && npm run dev"
    assert frontend_line == expected_frontend


def test_up_send_keys_exact_launch_line_without_env_file() -> None:
    """Without env_file_path, the source step is omitted."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    ctx = _make_ctx(env_file_path=None)
    svc = _make_service(tmux=tmux, hook_runner=hook)

    def _add_panes() -> None:
        tmux.seed_session("mp-alpha", {"0.0": 100, "0.1": 101})

    hook._side_effect = _add_panes

    svc.up(ctx)

    backend_line = next(line for _, t, line in tmux.sent if t == "0.0")
    expected = f"cd '{_WORKTREE}' && echo '=== backend ===' && npm run start:dev"
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
    expected = f"cd '{_WORKTREE}' && source '{_ENV_FILE}' && echo '=== backend ===' && npm run start:dev"
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
    assert line == f"cd '{_WORKTREE}' && echo '=== shell ==='"
