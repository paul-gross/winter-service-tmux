"""Tests for service_orchestrator.cli — name-addressed winter entrypoint door.

Covers:
- up/down still take ``[action, env]``
- status 0 patterns → status called for every running env
- status single literal ``alpha/backend`` → only that service
- status glob ``alpha/back*`` → matched subset
- status multi-pattern + cross-env ``*/backend`` (multiple envs)
- restart/logs with 0 patterns → non-zero exit
- restart multi-pattern → restart called per matched service
- logs → one logs call per matched env; query.services carries expanded names
- no-match pattern (any action) → non-zero exit + message
- ``-``-leading token tolerated as a pattern
- No args / bad action → exit 2
- Missing env / unreadable manifest → non-zero with message containing env name
- Exit-code passthrough from the service
- WINTER_WORKSPACE_DIR is passed to builder as workspace_root
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import Service, ServiceManifest, Target
from service_orchestrator.cli import main
from service_orchestrator.modules.orchestrate.dispatch_service import DispatchService
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.log_query import LogQuery
from service_orchestrator.modules.orchestrate.selector_service import SelectorService
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import WORKSPACE_TARGET
from tests.fakes import FakeLogService, FakeOrchestrator

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_CONFIG_DIR = _WORKSPACE / ".winter" / "config" / "winter-service-tmux"

# Two services so glob tests can distinguish subset matches
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
        env_file_path=None,
    )


def _make_workspace_ctx() -> SessionContext:
    """Build the expected workspace SessionContext (worktree_dir == workspace_root,
    services = workspace_services)."""
    return SessionContext(
        env=WORKSPACE_TARGET,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE,  # NOT _WORKSPACE/workspace
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.workspace_services,
        layout_hook=_MANIFEST.workspace_layout_hook,
        logs=_MANIFEST.logs,
        env_vars=None,
        env_file_path=None,
    )


class _FakeContainer:
    """Minimal fake Container with controllable tmux, builder, orchestrator, log_service.

    ``tmux.list_sessions()`` returns the list in ``sessions``.
    ``session_context_builder.build(env, ...)`` returns ``_make_ctx(env)`` unless
    ``build_raises`` is set, in which case it raises that exception.
    All action methods (up/down/status/restart/logs) record their calls and
    return ``service_rc`` / ``log_rc``.
    """

    session_context_builder: Any  # allow reassignment with different builder types

    def __init__(
        self,
        sessions: list[str] | None = None,
        service_rc: int = 0,
        log_rc: int = 0,
        build_raises: Exception | None = None,
    ) -> None:
        self._sessions = list(sessions) if sessions is not None else ["mp-alpha"]
        self._service_rc = service_rc
        self._log_rc = log_rc
        self._build_raises = build_raises

        # Build_call records (on the container for test access)
        self.build_calls: list[str] = []
        self.build_workspace_calls: list[Path | None] = []

        # tmux seam
        class _FakeTmux:
            def list_sessions(inner_self) -> list[str]:  # pyright: ignore[reportSelfClsParameterName]  — `inner_self` keeps the enclosing `self` reachable
                return list(self._sessions)

        self.tmux = _FakeTmux()

        # builder seam
        class _FakeBuilder:
            def build(inner_self, env: str, *, workspace_root=None) -> SessionContext:  # pyright: ignore[reportSelfClsParameterName]  — `inner_self` keeps the enclosing `self` reachable
                self.build_calls.append(env)
                if self._build_raises is not None:
                    raise self._build_raises
                return _make_ctx(env)

            def build_workspace(inner_self, *, workspace_root=None) -> SessionContext:  # pyright: ignore[reportSelfClsParameterName]  — `inner_self` keeps the enclosing `self` reachable
                self.build_workspace_calls.append(workspace_root)
                if self._build_raises is not None:
                    raise self._build_raises
                return _make_workspace_ctx()

        self.session_context_builder = _FakeBuilder()

        # orchestrator seam — promoted to FakeOrchestrator from fakes.py
        self.orchestrator = FakeOrchestrator(service_rc=service_rc)

        # log_service seam — promoted to FakeLogService from fakes.py
        self.log_service = FakeLogService(log_rc=log_rc)

    # Forward call records from inner fakes for backward-compatible test access
    @property
    def status_calls(self) -> list[tuple[SessionContext, tuple[str, ...]]]:
        return self.orchestrator.status_calls

    @property
    def restart_calls(self) -> list[tuple[SessionContext, str]]:
        return self.orchestrator.restart_calls

    @property
    def logs_calls(self) -> list[tuple[SessionContext, LogQuery]]:
        return self.log_service.logs_calls

    @property
    def up_calls(self) -> list[SessionContext]:
        return self.orchestrator.up_calls

    @property
    def down_calls(self) -> list[SessionContext]:
        return self.orchestrator.down_calls

    @property
    def follow_streams_calls(self) -> list[list]:
        return self.log_service.follow_streams_calls

    @property
    def selector(self) -> SelectorService:
        """Real SelectorService wired around the fake tmux and builder seams."""
        return SelectorService(self.tmux, self.session_context_builder)  # type: ignore[arg-type]

    @property
    def dispatch(self) -> DispatchService:
        """Real DispatchService wired around all fake seams."""
        return DispatchService(
            builder=self.session_context_builder,  # type: ignore[arg-type]
            orchestrator=self.orchestrator,  # type: ignore[arg-type]
            log_service=self.log_service,  # type: ignore[arg-type]
        )


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeContainer) -> _FakeContainer:
    """Patch ``service_orchestrator.cli.Container`` to return *fake*."""
    import service_orchestrator.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Container", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# No args / bad action → exit 2
# ---------------------------------------------------------------------------


def test_main_no_args_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_main_bad_action_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["badaction", "alpha"])
    assert rc == 2
    assert "badaction" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# up / down: single env arg unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["up", "down"])
def test_main_up_down_no_env_returns_2(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _FakeContainer())
    rc = main([action])
    assert rc == 2


@pytest.mark.parametrize("action", ["up", "down"])
def test_main_up_down_extra_arg_returns_2(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _FakeContainer())
    rc = main([action, "alpha", "extra"])
    assert rc == 2


@pytest.mark.parametrize("action", ["up", "down"])
def test_main_up_down_success(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeContainer(service_rc=0))
    rc = main([action, "alpha"])
    assert rc == 0
    if action == "up":
        assert len(fake.up_calls) == 1
        assert fake.up_calls[0].env == "alpha"
    else:
        assert len(fake.down_calls) == 1
        assert fake.down_calls[0].env == "alpha"


@pytest.mark.parametrize("action", ["up", "down"])
def test_main_up_down_passthrough_nonzero(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeContainer(service_rc=1))
    rc = main([action, "alpha"])
    assert rc == 1


# ---------------------------------------------------------------------------
# status: 0 patterns → all running envs
# ---------------------------------------------------------------------------


def test_main_status_zero_patterns_calls_status_for_all_running_envs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two running envs: alpha and beta
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha", "mp-beta"]),
    )
    rc = main(["status"])
    assert rc == 0
    envs_called = [ctx.env for ctx, _ in fake.status_calls]
    assert "alpha" in envs_called
    assert "beta" in envs_called
    assert len(fake.status_calls) == 2


def test_main_status_zero_patterns_no_sessions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _install(monkeypatch, _FakeContainer(sessions=[]))
    rc = main(["status"])
    assert rc == 0
    assert len(fake.status_calls) == 0


# ---------------------------------------------------------------------------
# status: literal pattern → only that service
# ---------------------------------------------------------------------------


def test_main_status_single_literal_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    rc = main(["status", "alpha/backend"])
    assert rc == 0
    assert len(fake.status_calls) == 1
    ctx, svcs = fake.status_calls[0]
    assert ctx.env == "alpha"
    assert svcs == ("backend",)


# ---------------------------------------------------------------------------
# status: glob pattern → matched subset
# ---------------------------------------------------------------------------


def test_main_status_glob_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "alpha/back*" should match "backend" but not "worker"
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    rc = main(["status", "alpha/back*"])
    assert rc == 0
    assert len(fake.status_calls) == 1
    ctx, svcs = fake.status_calls[0]
    assert ctx.env == "alpha"
    assert "backend" in svcs
    assert "worker" not in svcs


# ---------------------------------------------------------------------------
# status: cross-env pattern (*/backend)
# ---------------------------------------------------------------------------


def test_main_status_cross_env_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "*/backend" with two running envs → status called for both envs with only "backend"
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha", "mp-beta"]),
    )
    rc = main(["status", "*/backend"])
    assert rc == 0
    envs_called = {ctx.env: svcs for ctx, svcs in fake.status_calls}
    assert "alpha" in envs_called
    assert "beta" in envs_called
    assert envs_called["alpha"] == ("backend",)
    assert envs_called["beta"] == ("backend",)


# ---------------------------------------------------------------------------
# status: multi-pattern
# ---------------------------------------------------------------------------


def test_main_status_multi_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two patterns: "alpha/backend" and "beta/worker" → two distinct status calls
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha", "mp-beta"]),
    )
    rc = main(["status", "alpha/backend", "beta/worker"])
    assert rc == 0
    envs_called = {ctx.env: svcs for ctx, svcs in fake.status_calls}
    assert "alpha" in envs_called
    assert envs_called["alpha"] == ("backend",)
    assert "beta" in envs_called
    assert envs_called["beta"] == ("worker",)


# ---------------------------------------------------------------------------
# status: no-match pattern → non-zero exit
# ---------------------------------------------------------------------------


def test_main_status_no_match_pattern_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    rc = main(["status", "alpha/nonexistent"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "alpha/nonexistent" in err


# ---------------------------------------------------------------------------
# restart: 0 patterns → non-zero
# ---------------------------------------------------------------------------


def test_main_restart_zero_patterns_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _FakeContainer())
    rc = main(["restart"])
    assert rc == 1
    assert "restart" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# restart: multi-pattern → restart called per service
# ---------------------------------------------------------------------------


def test_main_restart_multi_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    rc = main(["restart", "alpha/backend", "alpha/worker"])
    assert rc == 0
    called_svcs = [(ctx.env, svc) for ctx, svc in fake.restart_calls]
    assert ("alpha", "backend") in called_svcs
    assert ("alpha", "worker") in called_svcs


def test_main_restart_glob_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "alpha/*" matches both backend and worker
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    rc = main(["restart", "alpha/*"])
    assert rc == 0
    called_svcs = [svc for _, svc in fake.restart_calls]
    assert "backend" in called_svcs
    assert "worker" in called_svcs


def test_main_restart_no_match_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    rc = main(["restart", "alpha/nonexistent"])
    assert rc != 0
    assert "alpha/nonexistent" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# logs: 0 patterns → non-zero
# ---------------------------------------------------------------------------


def test_main_logs_zero_patterns_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _FakeContainer())
    rc = main(["logs"])
    assert rc == 1
    assert "logs" in capsys.readouterr().err.lower()


def test_main_logs_flags_only_no_pattern_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Render flags with no positional pattern (winter's no-pattern dispatch) → rc 1.

    parse_log_args strips the flags, leaving zero patterns; the post-parse
    arity check in main() is the authoritative guard for this path.
    """
    _install(monkeypatch, _FakeContainer())
    rc = main(["logs", "--tail", "200"])
    assert rc == 1
    assert "logs" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# logs: one call per matched env; query.services carries expanded names
# ---------------------------------------------------------------------------


def test_main_logs_single_env_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))

    rc = main(["logs", "alpha/backend"])
    assert rc == 0
    assert len(fake.logs_calls) == 1
    ctx, query = fake.logs_calls[0]
    assert ctx.env == "alpha"
    assert query.services == ("backend",)


def test_main_logs_cross_env_pattern_one_call_per_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "*/backend" with alpha and beta running → one logs call per env
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha", "mp-beta"]),
    )

    rc = main(["logs", "*/backend"])
    assert rc == 0
    assert len(fake.logs_calls) == 2
    envs_called = {ctx.env for ctx, _ in fake.logs_calls}
    assert "alpha" in envs_called
    assert "beta" in envs_called
    for _, query in fake.logs_calls:
        assert query.services == ("backend",)


def test_main_logs_query_params_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))

    main(
        [
            "logs",
            "alpha/backend",
            "--tail",
            "50",
            "--since",
            "2026-01-01T00:00:00Z",
            "--until",
            "2026-12-31T00:00:00Z",
            "--timestamps",
        ]
    )

    assert len(fake.logs_calls) == 1
    _, query = fake.logs_calls[0]
    assert query.follow is False
    assert query.tail == 50
    assert query.since == "2026-01-01T00:00:00Z"
    assert query.until == "2026-12-31T00:00:00Z"
    assert query.timestamps is True


def test_main_logs_passthrough_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"], log_rc=1))

    rc = main(["logs", "alpha/backend"])
    assert rc == 1


def test_main_logs_no_match_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    rc = main(["logs", "alpha/nonexistent"])
    assert rc != 0
    assert "alpha/nonexistent" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# dash-leading token tolerated as a pattern (no flag parsing on this door)
# ---------------------------------------------------------------------------


def test_main_dash_leading_token_tolerated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # "-alpha/backend" has a leading dash; it should be treated as a pattern,
    # NOT as a flag that triggers argparse SystemExit.  The main() function
    # does no flag parsing on the pattern arguments, so a dash-prefixed token
    # is silently passed through as a literal pattern.
    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    try:
        rc = main(["status", "-alpha/backend"])
    except SystemExit:
        pytest.fail("dash-leading pattern raised SystemExit — flag parsing is happening")
    # Regardless of rc, the important property is no SystemExit was raised.
    # (The pattern "-alpha/backend" is concrete; it expands to env="-alpha"
    # which the fake builder resolves, so it may succeed or fail depending on
    # the fake session list — either outcome is acceptable as long as no
    # SystemExit is raised.)
    assert isinstance(rc, int)


# ---------------------------------------------------------------------------
# Missing / unreadable manifest → non-zero with env name in message
# ---------------------------------------------------------------------------


def test_main_manifest_error_contains_env_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeContainer(build_raises=ManifestError("committed manifest not found"))
    _install(monkeypatch, fake)
    rc = main(["up", "nonexistent-env"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "nonexistent-env" in err


def test_main_oserror_contains_env_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeContainer(build_raises=OSError("env file not readable"))
    _install(monkeypatch, fake)
    rc = main(["up", "missing-env"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "missing-env" in err


# ---------------------------------------------------------------------------
# WINTER_WORKSPACE_DIR is passed to builder as workspace_root
# ---------------------------------------------------------------------------


def test_main_uses_winter_workspace_dir_for_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", "/custom/ws")

    build_kwargs: list[dict] = []

    class _RecordingBuilder:
        def build(self, env: str, *, workspace_root=None) -> SessionContext:
            build_kwargs.append({"env": env, "workspace_root": workspace_root})
            return _make_ctx(env)

    fake = _FakeContainer()
    fake.session_context_builder = _RecordingBuilder()
    _install(monkeypatch, fake)

    main(["up", "alpha"])

    assert any(kw["workspace_root"] == Path("/custom/ws") for kw in build_kwargs)


def test_main_no_winter_workspace_dir_passes_none_for_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    build_kwargs: list[dict] = []

    class _RecordingBuilder:
        def build(self, env: str, *, workspace_root=None) -> SessionContext:
            build_kwargs.append({"env": env, "workspace_root": workspace_root})
            return _make_ctx(env)

    fake = _FakeContainer()
    fake.session_context_builder = _RecordingBuilder()
    _install(monkeypatch, fake)

    main(["up", "alpha"])

    assert any(kw["workspace_root"] is None for kw in build_kwargs)


# ---------------------------------------------------------------------------
# status entrypoint always emits a single env-keyed JSON document on stdout
# ---------------------------------------------------------------------------


def test_main_status_emits_envs_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The status entrypoint emits exactly one ``{"envs": [...]}`` document on
    stdout, aggregating every env in scope, regardless of any flag."""
    import json

    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha", "mp-beta"]))
    rc = main(["status"])
    assert rc == 0

    out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(out_lines) == 1
    doc = json.loads(out_lines[0])
    assert [e["env"] for e in doc["envs"]] == ["alpha", "beta"]


def test_main_status_no_sessions_emits_empty_envs_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No running sessions → a valid, non-error ``{"envs": []}`` document, rc 0."""
    import json

    _install(monkeypatch, _FakeContainer(sessions=[]))
    rc = main(["status"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"envs": []}


def test_main_status_orchestrator_error_folds_rc_but_still_emits_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An OrchestratorError while collecting a doc folds rc=1, yet stdout still
    carries a conformant document (winter never hits graceful degradation)."""
    import json

    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))

    def _raise(ctx: SessionContext, services: tuple[str, ...] = ()) -> dict:
        raise OrchestratorError("session gone")

    fake.orchestrator.status_env_document = _raise  # type: ignore[method-assign]

    rc = main(["status", "alpha/backend"])
    assert rc == 1

    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc == {"envs": []}
    assert "session gone" in captured.err


# ---------------------------------------------------------------------------
# logs -f (follow) → dispatches to follow_streams
# ---------------------------------------------------------------------------


def test_main_logs_follow_single_service_calls_follow_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logs -f selecting exactly one service → follow_streams called once with that (ctx, query) pair."""
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha"]),
    )

    rc = main(["logs", "alpha/backend", "-f"])

    assert rc == 0
    assert len(fake.logs_calls) == 0
    assert len(fake.follow_streams_calls) == 1
    streams = fake.follow_streams_calls[0]
    assert len(streams) == 1
    ctx, query = streams[0]
    assert ctx.env == "alpha"
    assert query.services == ("backend",)


def test_main_logs_follow_multi_service_single_env_calls_follow_streams_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logs -f with alpha/* (two services) → exactly one follow_streams call with both services."""
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha"]),
    )

    rc = main(["logs", "alpha/*", "-f"])

    assert rc == 0
    assert len(fake.logs_calls) == 0
    assert len(fake.follow_streams_calls) == 1
    streams = fake.follow_streams_calls[0]
    assert len(streams) == 1
    ctx, query = streams[0]
    assert ctx.env == "alpha"
    assert set(query.services) == {"backend", "worker"}


def test_main_logs_follow_cross_env_calls_follow_streams_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logs -f with */backend spanning alpha+beta → exactly one follow_streams call with two pairs."""
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha", "mp-beta"]),
    )

    rc = main(["logs", "*/backend", "-f"])

    assert rc == 0
    assert len(fake.logs_calls) == 0
    assert len(fake.follow_streams_calls) == 1
    streams = fake.follow_streams_calls[0]
    assert len(streams) == 2
    envs = {ctx.env for ctx, _ in streams}
    assert envs == {"alpha", "beta"}
    for _, query in streams:
        assert query.services == ("backend",)


def test_main_logs_follow_passthrough_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """follow_streams returning non-zero propagates to the exit code."""
    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"], log_rc=1))

    rc = main(["logs", "alpha/backend", "-f"])

    assert rc != 0


def test_main_logs_follow_tail_since_until_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """-f plus --tail/--since/--until argv flags are forwarded on the per-pair LogQuery objects."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))

    main(
        [
            "logs",
            "alpha/backend",
            "-f",
            "--tail",
            "50",
            "--since",
            "2026-01-01T00:00:00Z",
            "--until",
            "2026-12-31T00:00:00Z",
        ]
    )

    assert len(fake.follow_streams_calls) == 1
    streams = fake.follow_streams_calls[0]
    assert len(streams) == 1
    _ctx, query = streams[0]
    assert query.tail == 50
    assert query.since == "2026-01-01T00:00:00Z"
    assert query.until == "2026-12-31T00:00:00Z"


def test_main_logs_no_follow_multi_env_calls_logs_per_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logs without -f spanning multiple envs → one logs call per env."""
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha", "mp-beta"]),
    )

    rc = main(["logs", "*/backend"])

    assert rc == 0
    assert len(fake.logs_calls) == 2
    envs_called = {ctx.env for ctx, _ in fake.logs_calls}
    assert "alpha" in envs_called
    assert "beta" in envs_called


# ---------------------------------------------------------------------------
# workspace token — up / down (cli door)
# ---------------------------------------------------------------------------


def test_main_up_workspace_builds_workspace_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """up workspace → build_workspace called (NOT build('workspace',...)); orchestrator.up called."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-workspace"]))
    rc = main(["up", "workspace"])
    assert rc == 0
    # build_workspace was called, not build("workspace")
    assert len(fake.build_workspace_calls) == 1
    assert "workspace" not in fake.build_calls
    # orchestrator.up was called with the workspace ctx
    assert len(fake.up_calls) == 1
    assert fake.up_calls[0].env == WORKSPACE_TARGET
    assert fake.up_calls[0].worktree_dir == _WORKSPACE  # NOT _WORKSPACE/"workspace"


def test_main_down_workspace_builds_workspace_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """down workspace → build_workspace called; orchestrator.down called."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-workspace"]))
    rc = main(["down", "workspace"])
    assert rc == 0
    assert len(fake.build_workspace_calls) == 1
    assert "workspace" not in fake.build_calls
    assert len(fake.down_calls) == 1
    assert fake.down_calls[0].env == WORKSPACE_TARGET
    assert fake.down_calls[0].worktree_dir == _WORKSPACE


# ---------------------------------------------------------------------------
# workspace token — status with patterns (intercept before engine)
# ---------------------------------------------------------------------------


def test_main_status_workspace_bare_token_routes_to_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status workspace → workspace ctx built; all ws services passed."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-workspace"]))
    rc = main(["status", "workspace"])
    assert rc == 0
    assert len(fake.build_workspace_calls) >= 1
    assert "workspace" not in fake.build_calls
    assert len(fake.status_calls) == 1
    ctx, svcs = fake.status_calls[0]
    assert ctx.env == WORKSPACE_TARGET
    # "workspace" bare token → all workspace services
    ws_names = tuple(s.name for s in _MANIFEST.workspace_services)
    assert svcs == ws_names


def test_main_status_workspace_svc_glob_expands_against_workspace_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status workspace/ws-* → matches both ws-backend and ws-worker."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-workspace"]))
    rc = main(["status", "workspace/ws-*"])
    assert rc == 0
    assert len(fake.status_calls) == 1
    ctx, svcs = fake.status_calls[0]
    assert ctx.env == WORKSPACE_TARGET
    assert "ws-backend" in svcs
    assert "ws-worker" in svcs
    # env services must NOT appear
    assert "backend" not in svcs
    assert "worker" not in svcs


def test_main_status_workspace_specific_svc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status workspace/ws-backend → only ws-backend."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-workspace"]))
    rc = main(["status", "workspace/ws-backend"])
    assert rc == 0
    assert len(fake.status_calls) == 1
    _, svcs = fake.status_calls[0]
    assert svcs == ("ws-backend",)


# ---------------------------------------------------------------------------
# workspace token — restart
# ---------------------------------------------------------------------------


def test_main_restart_workspace_bare_token_restarts_all_workspace_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restart workspace → restarts all workspace services."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-workspace"]))
    rc = main(["restart", "workspace"])
    assert rc == 0
    assert len(fake.build_workspace_calls) >= 1
    assert "workspace" not in fake.build_calls
    restarted = [svc for _, svc in fake.restart_calls]
    ws_names = [s.name for s in _MANIFEST.workspace_services]
    for name in ws_names:
        assert name in restarted


def test_main_restart_workspace_glob_expands_against_workspace_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restart workspace/ws-* → restarts ws-backend and ws-worker."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-workspace"]))
    rc = main(["restart", "workspace/ws-*"])
    assert rc == 0
    restarted = [svc for _, svc in fake.restart_calls]
    assert "ws-backend" in restarted
    assert "ws-worker" in restarted
    assert "backend" not in restarted


# ---------------------------------------------------------------------------
# CRITICAL TEST (Risk #1): status with 0 patterns when BOTH env AND workspace sessions run
# ---------------------------------------------------------------------------


def test_main_status_zero_patterns_mixed_env_and_workspace_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE CRITICAL RISK #1 TEST.

    When both mp-alpha (env) and mp-workspace (workspace singleton) are running:
    - status with 0 patterns must include BOTH sessions
    - the workspace session must be built via build_workspace (worktree_dir=ws_root)
    - the env session must be built via build (worktree_dir=ws_root/alpha)
    - NO phantom ws_root/workspace context must be created
    """
    fake = _install(
        monkeypatch,
        _FakeContainer(sessions=["mp-alpha", "mp-workspace"]),
    )
    rc = main(["status"])
    assert rc == 0

    # Exactly two status calls — one for alpha, one for workspace
    assert len(fake.status_calls) == 2
    envs_called = {ctx.env for ctx, _ in fake.status_calls}
    assert "alpha" in envs_called
    assert WORKSPACE_TARGET in envs_called

    # Verify workspace ctx has worktree_dir == ws_root (NOT ws_root/workspace)
    ws_ctx = next(ctx for ctx, _ in fake.status_calls if ctx.env == WORKSPACE_TARGET)
    assert ws_ctx.worktree_dir == _WORKSPACE, (
        f"workspace ctx has wrong worktree_dir: {ws_ctx.worktree_dir!r}; "
        f"expected {_WORKSPACE!r} (must be ws_root, not ws_root/workspace)"
    )

    # build_workspace was called (for workspace session)
    assert len(fake.build_workspace_calls) >= 1

    # "workspace" must NOT appear in build_calls (no env-scoped build for workspace)
    assert "workspace" not in fake.build_calls, (
        "build('workspace', ...) was called — this is the Risk #1 bug: "
        "workspace session must be routed through build_workspace, not build"
    )

    # Env session ctx has worktree_dir == ws_root/alpha
    alpha_ctx = next(ctx for ctx, _ in fake.status_calls if ctx.env == "alpha")
    assert alpha_ctx.worktree_dir == _WORKSPACE / "alpha"


# ---------------------------------------------------------------------------
# REGRESSION: seed-selection bug — workspace FIRST in session list
# ---------------------------------------------------------------------------


def test_status_cross_env_glob_with_workspace_first_seeds_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for _read_manifest_context seed-selection bug.

    When list_sessions() returns ["mp-workspace", "mp-alpha"] (workspace FIRST),
    a cross-env all-glob pattern like "*/backend" must expand against the ENV
    (alpha) service catalog — not the workspace service catalog.

    Before the fix: the fallback loop picked the first session's env ("workspace")
    as the seed and called build_workspace(); the workspace manifest's services
    (ws-backend, ws-worker) were used as the catalog — so "backend" matched zero
    services and the call returned non-zero.

    After the fix: the loop skips the workspace session and uses "alpha" as the
    seed; the env manifest's services (backend, worker) are used — "*/backend"
    matches backend in alpha and the call succeeds.
    """
    fake = _install(
        monkeypatch,
        # workspace session listed FIRST — triggers the bug on un-patched code
        _FakeContainer(sessions=["mp-workspace", "mp-alpha"]),
    )
    rc = main(["status", "*/backend"])
    assert rc == 0, (
        "status '*/backend' should succeed when workspace is first in session list; "
        "got non-zero — seed was likely the workspace catalog (only ws-backend/ws-worker), "
        "so 'backend' matched nothing"
    )
    # The env (alpha) must have been called with "backend" from its catalog
    env_calls = [(ctx.env, svcs) for ctx, svcs in fake.status_calls if ctx.env != WORKSPACE_TARGET]
    assert len(env_calls) >= 1, "Expected at least one status call for the env (alpha)"
    assert any("backend" in svcs for _, svcs in env_calls), (
        "Expected 'backend' in the services passed to status for the env session"
    )
    # Workspace services must NOT appear as matched services (wrong catalog)
    all_svcs = [svc for _, svcs in fake.status_calls for svc in svcs]
    assert "ws-backend" not in all_svcs, (
        "ws-backend appeared in matched services — seed was wrongly the workspace catalog"
    )


# ---------------------------------------------------------------------------
# REGRESSION: orchestrator up <env> does NOT touch the workspace session
# ---------------------------------------------------------------------------


def test_up_normal_env_does_not_create_workspace_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the 'winter-cli owns the ensure, the orchestrator does not' seam.

    An orchestrator-side 'up alpha' (a normal env, NOT workspace) must issue
    NO workspace-session creation.  build_workspace must NOT be called, and
    the only up call must be for the env (alpha), not the workspace.

    This pins the seam: workspace-session ensure is winter-cli's responsibility
    (winter#65); the orchestrator does not auto-start workspace.
    """
    fake = _install(monkeypatch, _FakeContainer(service_rc=0))
    rc = main(["up", "alpha"])
    assert rc == 0
    # orchestrator.up called exactly once, for alpha
    assert len(fake.up_calls) == 1
    assert fake.up_calls[0].env == "alpha"
    # build_workspace must NOT have been called — orchestrator does not ensure workspace
    assert len(fake.build_workspace_calls) == 0, (
        "build_workspace was called during 'up alpha' — "
        "the orchestrator must not auto-ensure the workspace session; "
        "that is winter-cli's responsibility (winter#65)"
    )


def test_cli_up_passes_retry_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """cli.py 'up alpha' (the winter service up door) invokes orchestrator.up with retry=True."""
    fake = _install(monkeypatch, _FakeContainer(service_rc=0))
    rc = main(["up", "alpha"])
    assert rc == 0
    assert len(fake.orchestrator.up_calls) == 1
    assert fake.orchestrator.last_up_retry is True
