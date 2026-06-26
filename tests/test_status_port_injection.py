"""Phase 4 (winter#109) status port-injection tests.

These tests verify that the status path reads WINTER_PORT_BASE from the
PROCESS ENVIRONMENT (injected by core/winter-cli) and does NOT self-source
.winter.env.  Two invariants are checked:

1. Port-base injection: inject a sentinel WINTER_PORT_BASE in os.environ
   while the builder would normally supply a DIFFERENT value from the file;
   assert that the SessionContext used by the orchestrator has env_vars from
   os.environ — proving no self-sourcing.

2. Scope-from-pattern: given a core-supplied scope pattern (``alpha/*``) with
   NO live tmux session, the status path calls the orchestrator for alpha with
   a ctx whose env_vars reflect the injected port base — proving no live-session
   enumeration is needed.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from service_manifest.modules.manifest.model import Service, ServiceManifest, Target
from service_orchestrator.cli import main
from service_orchestrator.modules.orchestrate.dispatch_service import DispatchService
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
    workspace_services=(Service(name="ws-backend", target=Target(window=0, pane=0), cmd="ws-cmd"),),
)


def _make_ctx(env: str, env_vars: dict[str, str] | None = None) -> SessionContext:
    return SessionContext(
        env=env,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE / env,
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.services,
        layout_hook=_MANIFEST.layout_hook,
        logs=_MANIFEST.logs,
        env_vars=env_vars,
        inject_scope=env,
        env_file_path=None,
    )


def _make_workspace_ctx(env_vars: dict[str, str] | None = None) -> SessionContext:
    return SessionContext(
        env=WORKSPACE_TARGET,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE,
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.workspace_services,
        layout_hook=_MANIFEST.workspace_layout_hook,
        logs=_MANIFEST.logs,
        env_vars=env_vars,
        inject_scope=None,
        env_file_path=None,
    )


# ---------------------------------------------------------------------------
# Fake builder that records skip_env_file usage
# ---------------------------------------------------------------------------


class _RecordingBuilder:
    """Fake builder that records whether skip_env_file was passed for each build call.

    build() is called with skip_env_file=True on the status path (from
    _build_ctx) and with skip_env_file=False from selector.read_manifest_context
    and the non-status paths (up/down/restart/logs).

    The ctx returned always has env_vars=None (mirroring skip_env_file=True
    behaviour); DispatchService then injects os.environ via dataclasses.replace.
    build_workspace() similarly returns env_vars=None since build_for_target
    does not forward skip_env_file to it.
    """

    def __init__(self) -> None:
        self.build_calls: list[tuple[str, bool]] = []  # (env, skip_env_file)
        self.build_workspace_call_count: int = 0

    def build(self, env: str, *, workspace_root: Path | None = None, skip_env_file: bool = False) -> SessionContext:
        self.build_calls.append((env, skip_env_file))
        return _make_ctx(env, env_vars=None)

    def build_workspace(self, *, workspace_root: Path | None = None) -> SessionContext:
        self.build_workspace_call_count += 1
        return _make_workspace_ctx(env_vars=None)


# ---------------------------------------------------------------------------
# Capturing orchestrator that records ctx.env_vars on status calls
# ---------------------------------------------------------------------------


class _EnvCapturingOrchestrator(FakeOrchestrator):
    """FakeOrchestrator that records the env_vars dict seen in each status ctx."""

    def __init__(self) -> None:
        super().__init__()
        self.captured_env_vars: list[dict[str, str] | None] = []

    def status_env_document(self, ctx: SessionContext, services: tuple[str, ...] = ()) -> dict:  # type: ignore[override]
        self.captured_env_vars.append(dict(ctx.env_vars) if ctx.env_vars is not None else None)
        return super().status_env_document(ctx, services)


# ---------------------------------------------------------------------------
# Test 1: WINTER_PORT_BASE is read from os.environ, not the env file
# ---------------------------------------------------------------------------


def test_status_port_base_comes_from_process_env_not_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the STATUS path reads WINTER_PORT_BASE from os.environ, not .winter.env.

    The sentinel value 9999 is set only in os.environ (the file would have a
    different value, or be absent).  After _build_ctx replaces env_vars
    with dict(os.environ), the orchestrator's ctx.env_vars must contain 9999.
    """
    monkeypatch.setenv("WINTER_PORT_BASE", "9999")

    builder = _RecordingBuilder()
    capturing_orch = _EnvCapturingOrchestrator()

    dispatch = DispatchService(
        builder=builder,  # type: ignore[arg-type]
        orchestrator=capturing_orch,  # type: ignore[arg-type]
        log_service=FakeLogService(),  # type: ignore[arg-type]
        err_sink=StringIO(),
    )

    _docs, rc = dispatch.collect_status_env_services({"alpha": []}, _WORKSPACE)

    assert rc == 0
    assert len(capturing_orch.captured_env_vars) == 1
    env_vars = capturing_orch.captured_env_vars[0]
    assert env_vars is not None, "ctx.env_vars is None — os.environ injection did not run"

    # WINTER_PORT_BASE must be the sentinel from os.environ, not from any file
    assert env_vars.get("WINTER_PORT_BASE") == "9999", (
        f"ctx.env_vars['WINTER_PORT_BASE'] = {env_vars.get('WINTER_PORT_BASE')!r}; "
        "expected '9999' from the injected os.environ — status path may still be "
        "self-sourcing the env file"
    )

    # Also verify build was called with skip_env_file=True on the status path
    status_path_calls = [(env, skip) for env, skip in builder.build_calls if skip]
    assert len(status_path_calls) == 1, (
        f"Expected exactly one build call with skip_env_file=True (the status path); "
        f"got build_calls={builder.build_calls!r}"
    )
    assert status_path_calls[0][0] == "alpha"


def test_status_env_vars_contain_all_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ctx.env_vars on the status path is dict(os.environ) — all keys present."""
    monkeypatch.setenv("WINTER_PORT_BASE", "8888")
    monkeypatch.setenv("WINTER_ENV", "alpha")
    monkeypatch.setenv("WINTER_ENV_INDEX", "1")

    builder = _RecordingBuilder()
    capturing_orch = _EnvCapturingOrchestrator()

    dispatch = DispatchService(
        builder=builder,  # type: ignore[arg-type]
        orchestrator=capturing_orch,  # type: ignore[arg-type]
        log_service=FakeLogService(),  # type: ignore[arg-type]
        err_sink=StringIO(),
    )

    dispatch.collect_status_env_services({"alpha": []}, _WORKSPACE)

    assert capturing_orch.captured_env_vars
    env_vars = capturing_orch.captured_env_vars[0]
    assert env_vars is not None
    # All three core-injected vars must be visible
    assert env_vars.get("WINTER_PORT_BASE") == "8888"
    assert env_vars.get("WINTER_ENV") == "alpha"
    assert env_vars.get("WINTER_ENV_INDEX") == "1"


def test_status_no_port_base_in_process_env_gives_none_in_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When WINTER_PORT_BASE is not in os.environ, ctx.env_vars has no port_base."""
    monkeypatch.delenv("WINTER_PORT_BASE", raising=False)

    builder = _RecordingBuilder()
    capturing_orch = _EnvCapturingOrchestrator()

    dispatch = DispatchService(
        builder=builder,  # type: ignore[arg-type]
        orchestrator=capturing_orch,  # type: ignore[arg-type]
        log_service=FakeLogService(),  # type: ignore[arg-type]
        err_sink=StringIO(),
    )

    dispatch.collect_status_env_services({"alpha": []}, _WORKSPACE)

    assert capturing_orch.captured_env_vars
    env_vars = capturing_orch.captured_env_vars[0]
    assert env_vars is not None
    assert "WINTER_PORT_BASE" not in env_vars


# ---------------------------------------------------------------------------
# Test 2: Core-supplied scope pattern reports services without live-session
# enumeration; WINTER_PORT_BASE visible to the orchestrator from os.environ
# ---------------------------------------------------------------------------


def test_status_core_scope_pattern_no_live_session_needed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Given alpha/* with no live session, the orchestrator is called for alpha.

    This verifies:
    - Core supplies the scope (alpha/*) — no live-session enumeration needed.
    - WINTER_PORT_BASE from os.environ (4020) is visible in ctx.env_vars.
    - build() is called with skip_env_file=True on the status path.
    """
    import service_orchestrator.cli as cli_mod

    monkeypatch.setenv("WINTER_PORT_BASE", "4020")

    builder = _RecordingBuilder()
    capturing_orch = _EnvCapturingOrchestrator()

    # FakeTmuxRepository with NO running sessions (empty) to prove no
    # live-session enumeration is needed for the core-supplied scope.
    tmux = FakeTmuxRepository()  # no sessions

    class _FakeContainerForScope:
        def __init__(self) -> None:
            self._builder = builder
            self._orchestrator = capturing_orch
            self._log_service = FakeLogService()
            self._tmux = tmux

        @property
        def selector(self) -> SelectorService:
            return SelectorService(self._tmux, self._builder)  # type: ignore[arg-type]

        @property
        def dispatch(self) -> DispatchService:
            return DispatchService(
                builder=self._builder,  # type: ignore[arg-type]
                orchestrator=self._orchestrator,  # type: ignore[arg-type]
                log_service=self._log_service,  # type: ignore[arg-type]
            )

    fake_container = _FakeContainerForScope()
    monkeypatch.setattr(cli_mod, "Container", lambda: fake_container)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(_WORKSPACE))

    rc = main(["status", "alpha/*"])
    assert rc == 0

    # One status call for alpha (no live session required)
    assert len(capturing_orch.status_calls) == 1
    ctx, _ = capturing_orch.status_calls[0]
    assert ctx.env == "alpha"

    # ctx.env_vars must contain WINTER_PORT_BASE from os.environ
    assert capturing_orch.captured_env_vars
    env_vars = capturing_orch.captured_env_vars[0]
    assert env_vars is not None
    assert env_vars.get("WINTER_PORT_BASE") == "4020", (
        f"ctx.env_vars['WINTER_PORT_BASE'] = {env_vars.get('WINTER_PORT_BASE')!r}; "
        "expected '4020' from the injected os.environ"
    )

    # Verify a status document was emitted
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert "envs" in doc
    assert len(doc["envs"]) == 1
    assert doc["envs"][0]["env"] == "alpha"


def test_status_workspace_scope_reads_port_base_from_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workspace scope reads WINTER_WORKSPACE_PORT_BASE from os.environ via env_vars injection."""
    monkeypatch.setenv("WINTER_WORKSPACE_PORT_BASE", "7777")

    builder = _RecordingBuilder()
    capturing_orch = _EnvCapturingOrchestrator()

    dispatch = DispatchService(
        builder=builder,  # type: ignore[arg-type]
        orchestrator=capturing_orch,  # type: ignore[arg-type]
        log_service=FakeLogService(),  # type: ignore[arg-type]
        err_sink=StringIO(),
    )

    tmux = FakeTmuxRepository()
    selector = SelectorService(tmux, builder)  # type: ignore[arg-type]

    docs, rc = dispatch.collect_status_workspace(selector, ["workspace"], _WORKSPACE, "status")

    assert rc == 0
    assert len(docs) == 1

    # env_vars injected into the workspace ctx must contain WINTER_WORKSPACE_PORT_BASE from os.environ
    assert capturing_orch.captured_env_vars
    env_vars = capturing_orch.captured_env_vars[0]
    assert env_vars is not None
    assert env_vars.get("WINTER_WORKSPACE_PORT_BASE") == "7777", (
        f"workspace ctx.env_vars['WINTER_WORKSPACE_PORT_BASE'] = {env_vars.get('WINTER_WORKSPACE_PORT_BASE')!r}; "
        "expected '7777' from os.environ"
    )
    # build_workspace was called exactly once
    assert builder.build_workspace_call_count == 1
