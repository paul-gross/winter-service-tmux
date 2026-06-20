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
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.log_query import LogQuery

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")

# Two services so glob tests can distinguish subset matches
_MANIFEST = ServiceManifest(
    session_prefix="mp",
    env_file=".winter.env",
    layout_hook=None,
    services=(
        Service(name="backend", target=Target(window=0, pane=0), command="cmd"),
        Service(name="worker", target=Target(window=0, pane=1), command="cmd"),
    ),
    status_urls=(),
)


def _make_ctx(env: str = "alpha") -> EnvContext:
    return EnvContext(
        env=env,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE / env,
        manifest=_MANIFEST,
        env_vars=None,
        env_file_path=None,
    )


class _FakeContainer:
    """Minimal fake Container with controllable tmux, builder, orchestrator, log_service.

    ``tmux.list_sessions()`` returns the list in ``sessions``.
    ``env_context_builder.build(env, ...)`` returns ``_make_ctx(env)`` unless
    ``build_raises`` is set, in which case it raises that exception.
    All action methods (up/down/status/restart/logs) record their calls and
    return ``service_rc`` / ``log_rc``.
    """

    env_context_builder: Any  # allow reassignment with different builder types

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

        # Records
        self.status_calls: list[tuple[EnvContext, tuple[str, ...]]] = []
        self.restart_calls: list[tuple[EnvContext, str]] = []
        self.logs_calls: list[tuple[EnvContext, LogQuery]] = []
        self.up_calls: list[EnvContext] = []
        self.down_calls: list[EnvContext] = []
        self.build_calls: list[str] = []

        # tmux seam
        class _FakeTmux:
            def list_sessions(inner_self) -> list[str]:
                return list(self._sessions)

        self.tmux = _FakeTmux()

        # builder seam
        class _FakeBuilder:
            def build(inner_self, env: str, *, workspace_root=None) -> EnvContext:
                self.build_calls.append(env)
                if self._build_raises is not None:
                    raise self._build_raises
                return _make_ctx(env)

        self.env_context_builder = _FakeBuilder()

        # orchestrator seam
        class _FakeOrchestrator:
            def up(inner_self, ctx: EnvContext) -> int:
                self.up_calls.append(ctx)
                return self._service_rc

            def down(inner_self, ctx: EnvContext) -> int:
                self.down_calls.append(ctx)
                return self._service_rc

            def status(
                inner_self,
                ctx: EnvContext,
                services: tuple[str, ...] = (),
                *,
                json_output: bool = False,
            ) -> int:
                self.status_calls.append((ctx, services))
                return self._service_rc

            def restart(inner_self, ctx: EnvContext, service_name: str) -> int:
                self.restart_calls.append((ctx, service_name))
                return self._service_rc

        self.orchestrator = _FakeOrchestrator()

        self.follow_streams_calls: list[list] = []

        # log_service seam
        class _FakeLogService:
            def logs(inner_self, ctx: EnvContext, query: LogQuery) -> int:
                self.logs_calls.append((ctx, query))
                return self._log_rc

            def follow_streams(inner_self, streams) -> int:
                self.follow_streams_calls.append(list(streams))
                return self._log_rc

        self.log_service = _FakeLogService()


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


# ---------------------------------------------------------------------------
# logs: one call per matched env; query.services carries expanded names
# ---------------------------------------------------------------------------


def test_main_logs_single_env_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "0")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "0")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "0")
    monkeypatch.setenv("WINTER_LOG_TAIL", "50")
    monkeypatch.setenv("WINTER_LOG_SINCE", "2026-01-01T00:00:00Z")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "2026-12-31T00:00:00Z")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "1")

    main(["logs", "alpha/backend"])

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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "0")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

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
        def build(self, env: str, *, workspace_root=None) -> EnvContext:
            build_kwargs.append({"env": env, "workspace_root": workspace_root})
            return _make_ctx(env)

    fake = _FakeContainer()
    fake.env_context_builder = _RecordingBuilder()
    _install(monkeypatch, fake)

    main(["up", "alpha"])

    assert any(kw["workspace_root"] == Path("/custom/ws") for kw in build_kwargs)


def test_main_no_winter_workspace_dir_passes_none_for_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    build_kwargs: list[dict] = []

    class _RecordingBuilder:
        def build(self, env: str, *, workspace_root=None) -> EnvContext:
            build_kwargs.append({"env": env, "workspace_root": workspace_root})
            return _make_ctx(env)

    fake = _FakeContainer()
    fake.env_context_builder = _RecordingBuilder()
    _install(monkeypatch, fake)

    main(["up", "alpha"])

    assert any(kw["workspace_root"] is None for kw in build_kwargs)


# ---------------------------------------------------------------------------
# Exit-code passthrough for status
# ---------------------------------------------------------------------------


def test_main_status_passthrough_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"], service_rc=1))
    rc = main(["status", "alpha/backend"])
    assert rc == 1


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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "1")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

    rc = main(["logs", "alpha/backend"])

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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "1")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

    rc = main(["logs", "alpha/*"])

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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "1")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

    rc = main(["logs", "*/backend"])

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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "1")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

    rc = main(["logs", "alpha/backend"])

    assert rc != 0


def test_main_logs_follow_tail_since_until_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FOLLOW=1 + TAIL/SINCE/UNTIL env vars are forwarded on the per-pair LogQuery objects."""
    fake = _install(monkeypatch, _FakeContainer(sessions=["mp-alpha"]))
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "1")
    monkeypatch.setenv("WINTER_LOG_TAIL", "50")
    monkeypatch.setenv("WINTER_LOG_SINCE", "2026-01-01T00:00:00Z")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "2026-12-31T00:00:00Z")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

    main(["logs", "alpha/backend"])

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
    monkeypatch.setenv("WINTER_LOG_FOLLOW", "0")
    monkeypatch.setenv("WINTER_LOG_TAIL", "all")
    monkeypatch.setenv("WINTER_LOG_SINCE", "")
    monkeypatch.setenv("WINTER_LOG_UNTIL", "")
    monkeypatch.setenv("WINTER_LOG_TIMESTAMPS", "0")

    rc = main(["logs", "*/backend"])

    assert rc == 0
    assert len(fake.logs_calls) == 2
    envs_called = {ctx.env for ctx, _ in fake.logs_calls}
    assert "alpha" in envs_called
    assert "beta" in envs_called
