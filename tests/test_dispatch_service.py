"""Unit tests for DispatchService.

Constructs DispatchService with fake seams (FakeOrchestrator, FakeLogService,
and a minimal fake builder/tmux) to verify:
- RC passthrough from orchestrator/log_service
- Per-env OrchestratorError → rc=1 + ``orchestrate: env '<env>': <exc>`` to err_sink
- Up/down ManifestError → rc=1 + ``orchestrate: env '<env>': manifest error: <exc>``
- build_for_target workspace routing — workspace ctx goes through build_workspace,
  not build(env="workspace")
- Exit-code aggregation: last-non-zero-wins
- logs_follow empty-pairs returns rc or 1
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import Service, ServiceManifest, Target
from service_orchestrator.modules.orchestrate.dispatch_service import DispatchService
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.log_query import LogRenderOptions
from service_orchestrator.modules.orchestrate.selector_service import SelectorService
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import WORKSPACE_TARGET
from tests.conftest import FakeTmuxRepository
from tests.fakes import FakeLogService, FakeOrchestrator

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_CONFIG_DIR = _WORKSPACE / ".winter" / "config" / "winter-service-tmux"

_MANIFEST = ServiceManifest(
    session_prefix="mp",
    env_file=".winter.env",
    layout_hook=None,
    services=(
        Service(name="backend", target=Target(window=0, pane=0), cmd="cmd"),
        Service(name="worker", target=Target(window=0, pane=1), cmd="cmd"),
    ),
    workspace_services=(
        Service(name="ws-backend", target=Target(window=0, pane=0), cmd="ws-cmd"),
        Service(name="ws-worker", target=Target(window=0, pane=1), cmd="ws-cmd"),
    ),
)


def _make_ctx(env: str = "alpha") -> SessionContext:
    return SessionContext(
        env=env,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE / env,
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.services,
        layout_hook=_MANIFEST.layout_hook,
        logs=_MANIFEST.logs,
        env_vars=None,
        inject_scope=env,
        env_file_path=None,
    )


def _make_workspace_ctx() -> SessionContext:
    return SessionContext(
        env=WORKSPACE_TARGET,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE,
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.workspace_services,
        layout_hook=_MANIFEST.workspace_layout_hook,
        logs=_MANIFEST.logs,
        env_vars=None,
        inject_scope=None,
        env_file_path=None,
    )


# ---------------------------------------------------------------------------
# Fake builder
# ---------------------------------------------------------------------------


class _FakeBuilder:
    """Minimal builder that records calls and supports raise-on-build."""

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.build_calls: list[str] = []
        self.build_workspace_calls: list[Path | None] = []

    def build(self, env: str, *, workspace_root: Path | None = None, skip_env_file: bool = False) -> SessionContext:
        self.build_calls.append(env)
        if self._raises is not None:
            raise self._raises
        return _make_ctx(env)

    def build_workspace(self, *, workspace_root: Path | None = None) -> SessionContext:
        self.build_workspace_calls.append(workspace_root)
        if self._raises is not None:
            raise self._raises
        return _make_workspace_ctx()


def _make_dispatch(
    orchestrator: FakeOrchestrator | None = None,
    log_service: FakeLogService | None = None,
    builder: _FakeBuilder | None = None,
    err_sink: StringIO | None = None,
    service_rc: int = 0,
    log_rc: int = 0,
) -> tuple[DispatchService, _FakeBuilder, FakeOrchestrator, FakeLogService, StringIO]:
    """Build a DispatchService with fakes; returns (svc, builder, orch, log, err)."""
    b = builder or _FakeBuilder()
    o = orchestrator or FakeOrchestrator(service_rc=service_rc)
    ls = log_service or FakeLogService(log_rc=log_rc)
    err = err_sink if err_sink is not None else StringIO()
    svc = DispatchService(builder=b, orchestrator=o, log_service=ls, err_sink=err)  # type: ignore[arg-type]
    return svc, b, o, ls, err


# ---------------------------------------------------------------------------
# up / down: rc passthrough and manifest-error infix
# ---------------------------------------------------------------------------


def test_up_passthrough_rc() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch(service_rc=3)
    rc = svc.up("alpha", _WORKSPACE)
    assert rc == 3
    assert len(_o.up_calls) == 1
    assert _o.up_calls[0].env == "alpha"


def test_down_passthrough_rc() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch(service_rc=5)
    rc = svc.down("alpha", _WORKSPACE)
    assert rc == 5
    assert len(_o.down_calls) == 1


def test_up_manifest_error_prints_infix() -> None:
    """ManifestError on build → rc=1 + 'manifest error:' infix in message."""
    b = _FakeBuilder(raises=ManifestError("config file missing"))
    svc, _, _, _, err = _make_dispatch(builder=b)
    rc = svc.up("alpha", _WORKSPACE)
    assert rc == 1
    msg = err.getvalue()
    assert "manifest error:" in msg
    assert "alpha" in msg


def test_down_manifest_error_prints_infix() -> None:
    b = _FakeBuilder(raises=ManifestError("config file missing"))
    svc, _, _, _, err = _make_dispatch(builder=b)
    rc = svc.down("alpha", _WORKSPACE)
    assert rc == 1
    assert "manifest error:" in err.getvalue()


def test_up_oserror_no_manifest_infix() -> None:
    """OSError on build → rc=1, NO 'manifest error:' infix."""
    b = _FakeBuilder(raises=OSError("env file not readable"))
    svc, _, _, _, err = _make_dispatch(builder=b)
    rc = svc.up("alpha", _WORKSPACE)
    assert rc == 1
    msg = err.getvalue()
    assert "manifest error:" not in msg
    assert "alpha" in msg


def test_up_orchestrator_error_prints_env_line() -> None:
    """OrchestratorError from orchestrator.up → rc=1 + env line on err_sink."""
    err = StringIO()
    o = FakeOrchestrator()

    # Patch orchestrator.up to raise OrchestratorError
    def _raising_up(ctx: SessionContext, *, retry: bool = False) -> int:
        raise OrchestratorError("tmux failed")

    o.up = _raising_up  # type: ignore[method-assign]

    b = _FakeBuilder()
    svc = DispatchService(builder=b, orchestrator=o, log_service=FakeLogService(), err_sink=err)  # type: ignore[arg-type]
    rc = svc.up("alpha", _WORKSPACE)
    assert rc == 1
    msg = err.getvalue()
    assert "orchestrate: env 'alpha':" in msg
    assert "tmux failed" in msg


# ---------------------------------------------------------------------------
# up / down workspace: build_workspace called (not build("workspace"))
# ---------------------------------------------------------------------------


def test_up_workspace_routes_to_build_workspace() -> None:
    """up WORKSPACE_TARGET calls build_workspace, never build('workspace')."""
    svc, _b, _o, _ls, _err = _make_dispatch()
    rc = svc.up(WORKSPACE_TARGET, _WORKSPACE)
    assert rc == 0
    assert len(_b.build_workspace_calls) == 1
    assert WORKSPACE_TARGET not in _b.build_calls
    assert len(_o.up_calls) == 1
    assert _o.up_calls[0].env == WORKSPACE_TARGET
    assert _o.up_calls[0].worktree_dir == _WORKSPACE  # NOT _WORKSPACE/workspace


def test_down_workspace_routes_to_build_workspace() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    rc = svc.down(WORKSPACE_TARGET, _WORKSPACE)
    assert rc == 0
    assert len(_b.build_workspace_calls) == 1
    assert WORKSPACE_TARGET not in _b.build_calls


# ---------------------------------------------------------------------------
# collect_status_all_envs: doc collection, rc aggregation, error handling
# ---------------------------------------------------------------------------


def test_collect_status_all_envs_collects_one_doc_per_env() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    docs, rc = svc.collect_status_all_envs(["alpha", "beta"], _WORKSPACE)
    assert rc == 0
    assert len(_o.status_calls) == 2
    assert [d["env"] for d in docs] == ["alpha", "beta"]


def test_collect_status_all_envs_build_error_folds_rc() -> None:
    """Build error for one env → rc=1, env omitted, continues to next env."""
    err = StringIO()

    class _FlakeyBuilder:
        def build(self, env: str, *, workspace_root=None, skip_env_file: bool = False) -> SessionContext:
            if env == "alpha":
                raise OrchestratorError("session not found")
            return _make_ctx(env)

        def build_workspace(self, *, workspace_root=None) -> SessionContext:
            return _make_workspace_ctx()

    o = FakeOrchestrator()
    svc = DispatchService(builder=_FlakeyBuilder(), orchestrator=o, log_service=FakeLogService(), err_sink=err)  # type: ignore[arg-type]
    docs, rc = svc.collect_status_all_envs(["alpha", "beta"], _WORKSPACE)
    # alpha build fails (rc=1); beta still collected.
    assert rc == 1
    assert [d["env"] for d in docs] == ["beta"]
    assert len(o.status_calls) == 1  # only beta reached the orchestrator
    assert "alpha" in err.getvalue()


def test_collect_status_all_envs_orchestrator_error_folds_rc_and_omits_env() -> None:
    """OrchestratorError from one env → rc=1, that env omitted from docs."""
    err = StringIO()

    class _ErrOnAlpha:
        def status_env_document(self, ctx: SessionContext, services: tuple[str, ...] = ()) -> dict:
            if ctx.env == "alpha":
                raise OrchestratorError("session gone")
            return {"env": ctx.env, "session": ctx.session, "port_base": None, "services": []}

    svc = DispatchService(_FakeBuilder(), _ErrOnAlpha(), FakeLogService(), err)  # type: ignore[arg-type]
    docs, rc = svc.collect_status_all_envs(["alpha", "beta"], _WORKSPACE)
    assert rc == 1
    assert [d["env"] for d in docs] == ["beta"]
    assert "orchestrate: env 'alpha':" in err.getvalue()


# ---------------------------------------------------------------------------
# collect_status_env_services: per-env service tuple passed through
# ---------------------------------------------------------------------------


def test_collect_status_env_services_passes_svc_tuple() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    docs, rc = svc.collect_status_env_services({"alpha": ["backend"]}, _WORKSPACE)
    assert rc == 0
    assert len(_o.status_calls) == 1
    ctx, svcs = _o.status_calls[0]
    assert ctx.env == "alpha"
    assert svcs == ("backend",)
    assert [d["env"] for d in docs] == ["alpha"]


def test_collect_status_env_services_orchestrator_error_prints_env_line() -> None:
    err = StringIO()

    class _ErrOrchestrator:
        def status_env_document(self, ctx: SessionContext, services: tuple[str, ...] = ()) -> dict:
            raise OrchestratorError("session gone")

    svc = DispatchService(_FakeBuilder(), _ErrOrchestrator(), FakeLogService(), err)  # type: ignore[arg-type]
    docs, rc = svc.collect_status_env_services({"alpha": ["backend"]}, _WORKSPACE)
    assert rc == 1
    assert docs == []
    assert "orchestrate: env 'alpha':" in err.getvalue()


# ---------------------------------------------------------------------------
# restart_env_services: per-svc restart
# ---------------------------------------------------------------------------


def test_restart_env_services_calls_restart_per_svc() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    rc = svc.restart_env_services({"alpha": ["backend", "worker"]}, _WORKSPACE)
    assert rc == 0
    restarted = [(ctx.env, name) for ctx, name in _o.restart_calls]
    assert ("alpha", "backend") in restarted
    assert ("alpha", "worker") in restarted


def test_restart_env_services_rc_passthrough() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch(service_rc=7)
    rc = svc.restart_env_services({"alpha": ["backend"]}, _WORKSPACE)
    assert rc == 7


def test_restart_env_services_orchestrator_error_prints_env_line() -> None:
    err = StringIO()

    class _ErrOrchestrator:
        def restart(self, ctx: SessionContext, svc_name: str) -> int:
            raise OrchestratorError("restart failed")

    svc = DispatchService(_FakeBuilder(), _ErrOrchestrator(), FakeLogService(), err)  # type: ignore[arg-type]
    rc = svc.restart_env_services({"alpha": ["backend"]}, _WORKSPACE)
    assert rc == 1
    assert "orchestrate: env 'alpha':" in err.getvalue()
    assert "alpha" in err.getvalue()


# ---------------------------------------------------------------------------
# REGRESSION (Fix 1): workspace rc carried into env stage
# ---------------------------------------------------------------------------


def test_collect_status_env_services_carries_workspace_rc() -> None:
    """Workspace stage returns non-zero; env stage all-zero → final rc = workspace rc.

    The env stage must seed rc from ``current_rc`` (the workspace stage's rc)
    rather than re-seeding rc=0 internally, so a workspace failure survives a
    clean env stage.
    """
    svc, _b, _o, _ls, _err = _make_dispatch(service_rc=0)
    # Call with current_rc=3 (simulating workspace stage returning 3) and env stage
    # succeeding for every env.
    _docs, rc = svc.collect_status_env_services({"alpha": ["backend"]}, _WORKSPACE, current_rc=3)
    assert rc == 3, f"Expected workspace rc=3 to be carried; got {rc}"


def test_restart_env_services_carries_workspace_rc() -> None:
    """Workspace stage returns non-zero; env stage all-zero → final rc = workspace rc.

    This test FAILS before Fix 1 (restart_env_services re-seeds rc=0 internally)
    and PASSES after Fix 1 (current_rc parameter seeds rc instead).
    """
    svc, _b, _o, _ls, _err = _make_dispatch(service_rc=0)
    # Call with current_rc=5 (simulating workspace stage returning 5) and env stage
    # returning 0 for every service.
    rc = svc.restart_env_services({"alpha": ["backend"]}, _WORKSPACE, current_rc=5)
    assert rc == 5, f"Expected workspace rc=5 to be carried; got {rc}"


# ---------------------------------------------------------------------------
# collect_status_workspace: workspace routing
# ---------------------------------------------------------------------------


def test_collect_status_workspace_routes_build_workspace() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    tmux = FakeTmuxRepository()
    selector = SelectorService(tmux, _b)  # type: ignore[arg-type]
    docs, rc = svc.collect_status_workspace(selector, ["workspace"], _WORKSPACE, "status")
    assert rc == 0
    assert len(_b.build_workspace_calls) == 1
    assert WORKSPACE_TARGET not in _b.build_calls
    assert [d["env"] for d in docs] == [WORKSPACE_TARGET]


def test_collect_status_workspace_expands_svc_glob() -> None:
    """workspace/ws-* expands against workspace services."""
    svc, _b, _o, _ls, _err = _make_dispatch()
    tmux = FakeTmuxRepository()
    selector = SelectorService(tmux, _b)  # type: ignore[arg-type]
    docs, rc = svc.collect_status_workspace(selector, ["workspace/ws-*"], _WORKSPACE, "status")
    assert rc == 0
    assert len(docs) == 1
    assert len(_o.status_calls) == 1
    _, svcs = _o.status_calls[0]
    assert "ws-backend" in svcs
    assert "ws-worker" in svcs
    # env services must not appear
    assert "backend" not in svcs


def test_collect_status_workspace_dead_pattern_returns_1() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    tmux = FakeTmuxRepository()
    selector = SelectorService(tmux, _b)  # type: ignore[arg-type]
    docs, rc = svc.collect_status_workspace(selector, ["workspace/nonexistent"], _WORKSPACE, "status")
    assert rc == 1
    assert docs == []
    msg = _err.getvalue()
    assert "workspace/nonexistent" in msg


# ---------------------------------------------------------------------------
# restart_workspace
# ---------------------------------------------------------------------------


def test_restart_workspace_routes_build_workspace() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    tmux = FakeTmuxRepository()
    selector = SelectorService(tmux, _b)  # type: ignore[arg-type]
    rc = svc.restart_workspace(selector, ["workspace"], _WORKSPACE)
    assert rc == 0
    assert len(_b.build_workspace_calls) == 1
    assert WORKSPACE_TARGET not in _b.build_calls
    restarted = [name for _, name in _o.restart_calls]
    ws_names = [s.name for s in _MANIFEST.workspace_services]
    for name in ws_names:
        assert name in restarted


def test_restart_workspace_dead_pattern_returns_1() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    tmux = FakeTmuxRepository()
    selector = SelectorService(tmux, _b)  # type: ignore[arg-type]
    rc = svc.restart_workspace(selector, ["workspace/nonexistent"], _WORKSPACE)
    assert rc == 1
    assert "nonexistent" in _err.getvalue()


# ---------------------------------------------------------------------------
# logs_backlog: rc passthrough
# ---------------------------------------------------------------------------


def test_logs_backlog_passthrough_rc() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch(log_rc=4)
    _render = LogRenderOptions(follow=False, tail=None, since="", until="", timestamps=False)
    rc = svc.logs_backlog({"alpha": ["backend"]}, _WORKSPACE, _render)
    assert rc == 4
    assert len(_ls.logs_calls) == 1
    ctx, query = _ls.logs_calls[0]
    assert ctx.env == "alpha"
    assert query.services == ("backend",)


def test_logs_backlog_last_nonzero_wins() -> None:
    log_calls = []

    class _VariableLogService:
        def logs(self, ctx: SessionContext, query: object) -> int:
            log_calls.append(ctx.env)
            return 1 if ctx.env == "alpha" else 2

    err = StringIO()
    svc = DispatchService(_FakeBuilder(), FakeOrchestrator(), _VariableLogService(), err)  # type: ignore[arg-type]
    _render = LogRenderOptions(follow=False, tail=None, since="", until="", timestamps=False)
    rc = svc.logs_backlog({"alpha": ["backend"], "beta": ["backend"]}, _WORKSPACE, _render)
    assert rc == 2


# ---------------------------------------------------------------------------
# logs_follow: empty pairs → rc or 1; result passthrough
# ---------------------------------------------------------------------------


def test_logs_follow_empty_pairs_returns_rc_or_1() -> None:
    """Build fails for all envs → empty pairs → return current_rc or 1."""
    b = _FakeBuilder(raises=OrchestratorError("no session"))
    svc, _b2, _o2, _ls2, _err2 = _make_dispatch(builder=b)
    _render = LogRenderOptions(follow=True, tail=None, since="", until="", timestamps=False)
    # current_rc=0 → rc or 1 = 1
    rc = svc.logs_follow({"alpha": ["backend"]}, _WORKSPACE, _render, current_rc=0)
    assert rc == 1


def test_logs_follow_no_envs_at_all_returns_current_rc_or_1() -> None:
    """Empty env_services dict (no envs to iterate) → current_rc or 1."""
    svc, _b2, _o2, _ls2, _err2 = _make_dispatch()
    _render = LogRenderOptions(follow=True, tail=None, since="", until="", timestamps=False)
    # Empty dict → no build attempts, rc stays at current_rc=0 → rc or 1 = 1
    rc = svc.logs_follow({}, _WORKSPACE, _render, current_rc=0)
    assert rc == 1


def test_logs_follow_result_if_nonzero_else_rc() -> None:
    """follow_streams returning non-zero propagates; if zero, use current_rc."""
    svc, _b, _o, _ls, _err = _make_dispatch(log_rc=130)
    _render = LogRenderOptions(follow=True, tail=None, since="", until="", timestamps=False)
    rc = svc.logs_follow({"alpha": ["backend"]}, _WORKSPACE, _render, current_rc=0)
    assert rc == 130  # result is non-zero so result wins


def test_logs_follow_zero_result_uses_current_rc() -> None:
    """follow_streams returning 0, current_rc=1 → return 0 (result wins when non-zero, else rc)."""
    svc, _b, _o, _ls, _err = _make_dispatch(log_rc=0)
    _render = LogRenderOptions(follow=True, tail=None, since="", until="", timestamps=False)
    rc = svc.logs_follow({"alpha": ["backend"]}, _WORKSPACE, _render, current_rc=1)
    # result=0 so "result if result != 0 else rc" → rc=1
    assert rc == 1


def test_logs_follow_calls_follow_streams_with_pairs() -> None:
    svc, _b, _o, _ls, _err = _make_dispatch()
    _render = LogRenderOptions(follow=True, tail=None, since="", until="", timestamps=False)
    rc = svc.logs_follow({"alpha": ["backend", "worker"]}, _WORKSPACE, _render)
    assert rc == 0
    assert len(_ls.follow_streams_calls) == 1
    streams = _ls.follow_streams_calls[0]
    assert len(streams) == 1  # one (ctx, query) pair per env
    ctx, query = streams[0]
    assert ctx.env == "alpha"
    assert set(query.services) == {"backend", "worker"}


# ---------------------------------------------------------------------------
# up: retry=True wired through DispatchService
# ---------------------------------------------------------------------------


def test_up_passes_retry_true_to_orchestrator() -> None:
    """DispatchService.up invokes orchestrator.up with retry=True."""
    svc, _b, _o, _ls, _err = _make_dispatch()
    rc = svc.up("alpha", _WORKSPACE)
    assert rc == 0
    assert len(_o.up_calls) == 1
    assert _o.last_up_retry is True
