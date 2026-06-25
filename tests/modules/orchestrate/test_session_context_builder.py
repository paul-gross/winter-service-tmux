"""Tests for SessionContextBuilder.build_workspace() and build_for_target().

Covers:
- build_workspace: worktree_dir==workspace_root, env=="workspace",
  session=="<prefix>-workspace", env_vars is None, env_file_path is None,
  ctx.services == the manifest's workspace_services (scope selected directly).
- build_for_target: routes "workspace" to build_workspace, routes any other
  name to build().
- End-to-end: OrchestratorService.up/down/status driven with a
  build_workspace-produced ctx against FakeTmuxRepository — proves the
  scope-agnostic SessionContext drives the orchestrator unchanged.

Note: reader support for workspace services (a [[service]] entry with
scope = "workspace") and workspace_layout_hook is exercised by test_reader.py.
Most tests here populate workspace_services / workspace_layout_hook directly via
the model dataclass to stay decoupled from the config-parse layer;
test_build_workspace_selects_workspace_fields exercises the reader->builder chain
whole via a scope-tagged TOML.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import service_manifest.container as sm_container_mod
from service_manifest.modules.manifest.model import (
    LogConfig,
    Service,
    ServiceManifest,
    Target,
)
from service_orchestrator.modules.orchestrate.orchestrator_service import OrchestratorService
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import (
    WORKSPACE_TARGET,
    SessionContextBuilder,
    build_for_target,
)
from tests.conftest import (
    FakeLayoutHookRunner,
    FakeLogRepository,
    FakeProcessReaper,
    FakeTmuxRepository,
    FakeWorkspaceLocator,
)
from tests.fakes import FakeFilesystemReader

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_CONFIG_DIR = _WORKSPACE / ".winter" / "config" / "winter-service-tmux"
_MANIFEST_COMMITTED_PATH = Path("config.toml")

# A minimal TOML manifest with env services only.  These tests populate
# workspace_services / workspace_layout_hook directly via the model (rather than
# through the config parse layer) to stay focused on builder behaviour.
_MANIFEST_TOML_ENV_ONLY = """\
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

# Workspace services for direct model construction.
_WS_SERVICES = (
    Service(name="monitor", target=Target(window=0, pane=0), cmd="python -m monitor"),
    Service(name="proxy", target=Target(window=0, pane=1), cmd="nginx -g 'daemon off;'"),
)
_WS_LAYOUT_HOOK = "workspace-layout-hook.sh"


def _make_sm_container(toml_content: str = _MANIFEST_TOML_ENV_ONLY) -> sm_container_mod.Container:
    """Build a service_manifest.Container seeded with in-memory TOML."""
    abs_toml_path = _CONFIG_DIR / _MANIFEST_COMMITTED_PATH
    fake_fs = FakeFilesystemReader({abs_toml_path: toml_content})
    return sm_container_mod.Container(fs=fake_fs)


def _make_builder(toml_content: str = _MANIFEST_TOML_ENV_ONLY) -> SessionContextBuilder:
    """Build an SessionContextBuilder with fakes wired up."""
    sm = _make_sm_container(toml_content)
    locator = FakeWorkspaceLocator(_WORKSPACE)
    return SessionContextBuilder(
        locator=locator,
        manifest_reader=sm.manifest_reader,
        env_reader=sm.env_reader,
    )


def _make_workspace_manifest(
    workspace_services: tuple[Service, ...] = _WS_SERVICES,
    workspace_layout_hook: str | None = _WS_LAYOUT_HOOK,
) -> ServiceManifest:
    """Construct a ServiceManifest with workspace fields populated directly.

    Used for E2E orchestrator tests — keeps them decoupled from the config-parse layer.
    """
    return ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(),  # env services cleared
        logs=LogConfig(),
        workspace_services=workspace_services,
        workspace_layout_hook=workspace_layout_hook,
    )


def _make_workspace_ctx(
    workspace_services: tuple[Service, ...] = _WS_SERVICES,
    workspace_layout_hook: str | None = _WS_LAYOUT_HOOK,
) -> SessionContext:
    """Build a workspace SessionContext directly (bypassing the reader for parse-layer independence).

    Selects the manifest's workspace_* fields into the scope-agnostic SessionContext,
    exactly as ``build_workspace()`` does — no env-shaped projection.
    """
    manifest = _make_workspace_manifest(
        workspace_services=workspace_services,
        workspace_layout_hook=workspace_layout_hook,
    )
    return SessionContext(
        env=WORKSPACE_TARGET,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE,
        config_dir=_CONFIG_DIR,
        session_prefix=manifest.session_prefix,
        services=manifest.workspace_services,
        layout_hook=manifest.workspace_layout_hook,
        logs=manifest.logs,
        env_vars=None,
        env_file_path=None,
    )


def _make_orchestrator_service(
    tmux: FakeTmuxRepository | None = None,
    reaper: FakeProcessReaper | None = None,
    hook_runner: FakeLayoutHookRunner | None = None,
    log_repo: FakeLogRepository | None = None,
) -> OrchestratorService:
    return OrchestratorService(
        tmux=tmux or FakeTmuxRepository(),
        reaper=reaper or FakeProcessReaper(),
        hook_runner=hook_runner or FakeLayoutHookRunner(),
        log_repo=log_repo or FakeLogRepository(),
    )


# ---------------------------------------------------------------------------
# WORKSPACE_TARGET constant
# ---------------------------------------------------------------------------


def test_workspace_target_constant_is_workspace() -> None:
    assert WORKSPACE_TARGET == "workspace"


# ---------------------------------------------------------------------------
# build() and build_workspace() — config_dir threaded through
# ---------------------------------------------------------------------------


def test_build_config_dir_from_locator() -> None:
    """build() sets ctx.config_dir from locator.config_dir()."""
    builder = _make_builder()
    ctx = builder.build("alpha")
    assert ctx.config_dir == _CONFIG_DIR


def test_build_workspace_config_dir_from_locator() -> None:
    """build_workspace() sets ctx.config_dir from locator.config_dir()."""
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.config_dir == _CONFIG_DIR


# ---------------------------------------------------------------------------
# build_workspace — SessionContext field assertions
# ---------------------------------------------------------------------------


def test_build_workspace_env_is_workspace() -> None:
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.env == "workspace"


def test_build_workspace_worktree_dir_equals_workspace_root() -> None:
    """THE load-bearing fork: worktree_dir must be ws_root, NOT ws_root/workspace."""
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.worktree_dir == _WORKSPACE
    assert ctx.worktree_dir == ctx.workspace_root


def test_build_workspace_workspace_root_resolved_from_locator() -> None:
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.workspace_root == _WORKSPACE


def test_build_workspace_workspace_root_override() -> None:
    """workspace_root kwarg overrides the workspace_root/worktree_dir but not config_dir."""
    override = Path("/other/root")
    # Manifest is read from locator.config_dir() (_CONFIG_DIR), not from override.
    locator = FakeWorkspaceLocator(_WORKSPACE)
    sm = _make_sm_container()
    builder = SessionContextBuilder(
        locator=locator,
        manifest_reader=sm.manifest_reader,
        env_reader=sm.env_reader,
    )
    ctx = builder.build_workspace(workspace_root=override)
    assert ctx.workspace_root == override
    assert ctx.worktree_dir == override


def test_build_workspace_env_vars_is_none() -> None:
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.env_vars is None


def test_build_workspace_env_file_path_is_none() -> None:
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.env_file_path is None


def test_build_workspace_session_is_prefix_workspace() -> None:
    """SessionContext.session == f"{prefix}-workspace" for free — no change to SessionContext."""
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.session == "mp-workspace"


def test_build_workspace_drops_env_services() -> None:
    """The workspace session selects no env services.

    The env-only TOML declares env services but no workspace-scoped service,
    so build_workspace() yields empty services — it reads the workspace scope,
    not the env scope.
    """
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.services == ()


def test_build_workspace_session_prefix_preserved() -> None:
    builder = _make_builder()
    ctx = builder.build_workspace()
    assert ctx.session_prefix == "mp"


def test_build_workspace_selects_workspace_fields() -> None:
    """End-to-end: build_workspace() surfaces the manifest's workspace_* fields as the
    session's services/layout_hook — no env-shaped projection.

    Uses a real scope-tagged TOML so the reader -> builder -> SessionContext chain is
    exercised whole.
    """
    toml = """\
session_prefix = "mp"
workspace_layout_hook = "workspace-layout-hook.sh"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"

[[service]]
name = "monitor"
target = "0.0"
cmd = "python -m monitor"
scope = "workspace"
"""
    builder = _make_builder(toml)
    ctx = builder.build_workspace()
    assert [s.name for s in ctx.services] == ["monitor"]
    assert ctx.layout_hook == "workspace-layout-hook.sh"
    assert ctx.session_prefix == "mp"


# ---------------------------------------------------------------------------
# build_for_target — dispatcher
# ---------------------------------------------------------------------------


def test_build_for_target_workspace_calls_build_workspace() -> None:
    builder = _make_builder()
    ctx = build_for_target(builder, WORKSPACE_TARGET)
    assert ctx.env == WORKSPACE_TARGET
    assert ctx.worktree_dir == _WORKSPACE


def test_build_for_target_env_name_calls_build() -> None:
    builder = _make_builder()
    ctx = build_for_target(builder, "alpha")
    assert ctx.env == "alpha"
    assert ctx.worktree_dir == _WORKSPACE / "alpha"


def test_build_for_target_workspace_root_forwarded_to_build_workspace() -> None:
    # workspace_root override propagates; manifest is still read from locator.config_dir().
    override = Path("/other/root")
    locator = FakeWorkspaceLocator(_WORKSPACE)
    sm = _make_sm_container()
    builder = SessionContextBuilder(
        locator=locator,
        manifest_reader=sm.manifest_reader,
        env_reader=sm.env_reader,
    )
    ctx = build_for_target(builder, WORKSPACE_TARGET, workspace_root=override)
    assert ctx.workspace_root == override
    assert ctx.worktree_dir == override


def test_build_for_target_workspace_root_forwarded_to_build() -> None:
    """workspace_root kwarg reaches build() for non-workspace targets."""
    override = Path("/other/root")
    locator = FakeWorkspaceLocator(_WORKSPACE)
    sm = _make_sm_container()
    builder = SessionContextBuilder(
        locator=locator,
        manifest_reader=sm.manifest_reader,
        env_reader=sm.env_reader,
    )
    ctx = build_for_target(builder, "beta", workspace_root=override)
    assert ctx.workspace_root == override
    assert ctx.worktree_dir == override / "beta"


# ---------------------------------------------------------------------------
# End-to-end: OrchestratorService driven by a workspace-flavored SessionContext
# ---------------------------------------------------------------------------
# These tests prove the load-bearing simplification: OrchestratorService.up/down/status
# all work unchanged when fed a workspace-flavored SessionContext.  The ctx is constructed
# directly (bypassing the reader) so there's no Phase 2 dependency.
# ---------------------------------------------------------------------------


def test_e2e_up_creates_workspace_session() -> None:
    """up() with workspace ctx creates the <prefix>-workspace tmux session."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()

    def _add_panes() -> None:
        tmux.seed_session("mp-workspace", {"0.0": 200, "0.1": 201})

    hook._side_effect = _add_panes

    ctx = _make_workspace_ctx()
    svc = _make_orchestrator_service(tmux=tmux, hook_runner=hook)

    result = svc.up(ctx)

    assert result == 0
    assert "mp-workspace" in tmux._sessions


def test_e2e_up_runs_workspace_layout_hook_with_ws_root_cwd() -> None:
    """Hook is invoked with cwd=workspace_root and WINTER_TMUX_WORKTREE_DIR=workspace_root."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()

    def _add_panes() -> None:
        tmux.seed_session("mp-workspace", {"0.0": 200, "0.1": 201})

    hook._side_effect = _add_panes

    ctx = _make_workspace_ctx()
    svc = _make_orchestrator_service(tmux=tmux, hook_runner=hook)
    svc.up(ctx)

    assert len(hook.calls) == 1
    hook_path, hook_env, hook_cwd = hook.calls[0]
    # cwd should be workspace_root, not workspace_root/workspace
    assert hook_cwd == _WORKSPACE
    assert hook_env["WINTER_TMUX_SESSION"] == "mp-workspace"
    assert hook_env["WINTER_TMUX_WORKTREE_DIR"] == str(_WORKSPACE)
    assert hook_env["WINTER_ENV"] == "workspace"
    assert hook_path == _CONFIG_DIR / _WS_LAYOUT_HOOK


def test_e2e_up_sends_workspace_service_commands() -> None:
    """up() sends commands for workspace_services (monitor, proxy), not env services."""
    tmux = FakeTmuxRepository()
    hook = FakeLayoutHookRunner()
    log_repo = FakeLogRepository()

    def _add_panes() -> None:
        tmux.seed_session("mp-workspace", {"0.0": 200, "0.1": 201})

    hook._side_effect = _add_panes

    ctx = _make_workspace_ctx()
    svc = _make_orchestrator_service(tmux=tmux, hook_runner=hook, log_repo=log_repo)
    svc.up(ctx)

    assert len(tmux.sent) == 2
    targets = {t for _, t, _ in tmux.sent}
    assert targets == {"0.0", "0.1"}

    # monitor command line should reference workspace root as cwd
    monitor_line = next(line for _, t, line in tmux.sent if t == "0.0")
    assert shlex.quote(str(_WORKSPACE)) in monitor_line
    assert "python -m monitor" in monitor_line

    # ensure_log_dir must be called with workspace root (worktree_dir==ws_root)
    assert log_repo.ensure_log_dir_calls == [_WORKSPACE]


def test_e2e_down_reaps_and_kills_workspace_session() -> None:
    """down() with workspace ctx reaps children and kills <prefix>-workspace."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-workspace", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(descendant_map={10: [100], 20: [200]})

    ctx = _make_workspace_ctx()
    svc = _make_orchestrator_service(tmux=tmux, reaper=reaper)

    result = svc.down(ctx)

    assert result == 0
    assert "mp-workspace" not in tmux._sessions
    assert "mp-workspace" in tmux.killed_sessions
    assert len(reaper.killed) == 1
    assert set(reaper.killed[0]) == {100, 200}


def test_e2e_status_lists_workspace_services(capsys) -> None:  # type: ignore[no-untyped-def]
    """status() with workspace ctx lists the workspace services (monitor, proxy)."""
    tmux = FakeTmuxRepository()
    tmux.seed_session("mp-workspace", {"0.0": 10, "0.1": 20})
    reaper = FakeProcessReaper(children_set={10})  # monitor running, proxy stopped

    ctx = _make_workspace_ctx()
    svc = _make_orchestrator_service(tmux=tmux, reaper=reaper)

    result = svc.status(ctx)

    assert result == 0
    captured = capsys.readouterr()
    # workspace services (not env services) are reported
    assert "monitor" in captured.out
    assert "proxy" in captured.out
    assert "running" in captured.out
    assert "stopped" in captured.out
    # env services must NOT appear
    assert "backend" not in captured.out
    assert "frontend" not in captured.out


def test_e2e_status_no_workspace_session(capsys) -> None:  # type: ignore[no-untyped-def]
    """status() when no workspace session is running reports the session name."""
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()

    ctx = _make_workspace_ctx()
    svc = _make_orchestrator_service(tmux=tmux, reaper=reaper)

    result = svc.status(ctx)

    assert result == 0
    captured = capsys.readouterr()
    assert "mp-workspace" in captured.out
