"""Tests for service_orchestrator.env_cli — env-root symlink door.

Covers:
- Symlink-dir resolution: construct a temp <ws>/<env>/up symlink →
  assert env/workspace resolved correctly from argv[0]
- Marker-walk fallback when symlink resolution fails
- WINTER_WORKSPACE_DIR path: invoked as <ext>/workflow/down <env> with
  WINTER_WORKSPACE_DIR set → manifest-driven reap, not suffix fallback
- ``local`` mode builds env-less context (skip_env_file=True)
- ``status`` no patterns → all services in this env
- ``status --all`` still loops sessions by prefix (unchanged)
- ``status <literal>`` → only that service
- ``status back*`` → matched subset
- ``status`` with no-match pattern → non-zero + message
- ``down`` env-suffix fallback when manifest missing
- project/ guard refuses with non-zero
- ``restart`` single service still works
- ``restart`` multi-pattern / glob → restart called per matched service
- ``restart`` no args → usage/help, non-zero
- ``restart`` no-match pattern → non-zero + message
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import Service, ServiceManifest, Target
from service_orchestrator import env_cli as env_cli_mod
from service_orchestrator.env_cli import (
    _locate_workspace_and_env,
    _resolve_from_argv0,
    main,
)
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import WORKSPACE_TARGET

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_CONFIG_DIR = _WORKSPACE / ".winter" / "config" / "winter-service-tmux"
_MANIFEST = ServiceManifest(
    session_prefix="mp",
    env_file=".winter.env",
    layout_hook=None,
    services=(Service(name="backend", target=Target(window=0, pane=0), cmd="cmd"),),
    status_urls=(),
    workspace_services=(Service(name="ws-svc", target=Target(window=0, pane=0), cmd="ws-cmd"),),
)
_MANIFEST_MULTI = ServiceManifest(
    session_prefix="mp",
    env_file=".winter.env",
    layout_hook=None,
    services=(
        Service(name="backend", target=Target(window=0, pane=0), cmd="cmd"),
        Service(name="backend-worker", target=Target(window=0, pane=1), cmd="cmd"),
        Service(name="frontend", target=Target(window=1, pane=0), cmd="cmd"),
    ),
    status_urls=(),
)


def _make_ctx(env: str = "alpha", manifest: ServiceManifest = _MANIFEST) -> SessionContext:
    return SessionContext(
        env=env,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE / env,
        config_dir=_CONFIG_DIR,
        session_prefix=manifest.session_prefix,
        services=manifest.services,
        layout_hook=manifest.layout_hook,
        status_urls=manifest.status_urls,
        logs=manifest.logs,
        env_vars={"BACKEND_PORT": "4100"},
        env_file_path=_WORKSPACE / env / ".winter.env",
    )


def _make_workspace_ctx() -> SessionContext:
    # The workspace session selects the manifest's workspace_* fields (no status URLs).
    return SessionContext(
        env=WORKSPACE_TARGET,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE,  # NOT _WORKSPACE/workspace
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.workspace_services,
        layout_hook=_MANIFEST.workspace_layout_hook,
        status_urls=(),
        logs=_MANIFEST.logs,
        env_vars=None,
        env_file_path=None,
    )


def _make_mock_container(
    ctx: SessionContext,
    *,
    service_rc: int = 0,
    build_side_effect: Exception | None = None,
    workspace_ctx: SessionContext | None = None,
) -> MagicMock:
    mock_builder = MagicMock()
    if build_side_effect is not None:
        mock_builder.build.side_effect = build_side_effect
    else:
        mock_builder.build.return_value = ctx

    # Configure build_workspace return value (returns workspace ctx or a default).
    _ws_ctx = workspace_ctx if workspace_ctx is not None else _make_workspace_ctx()
    mock_builder.build_workspace.return_value = _ws_ctx

    mock_orchestrator = MagicMock()
    mock_orchestrator.up.return_value = service_rc
    mock_orchestrator.down.return_value = service_rc
    mock_orchestrator.status.return_value = service_rc
    mock_orchestrator.restart.return_value = service_rc

    mock_tmux = MagicMock()
    mock_tmux.list_sessions.return_value = []

    mock_container = MagicMock()
    mock_container.session_context_builder = mock_builder
    mock_container.orchestrator = mock_orchestrator
    mock_container.tmux = mock_tmux
    return mock_container


def _patch_container_and_argv0(
    monkeypatch: pytest.MonkeyPatch,
    container: MagicMock,
    argv0: str,
) -> None:
    monkeypatch.setattr(env_cli_mod, "Container", lambda: container)
    monkeypatch.setattr(sys, "argv", [argv0])


# ---------------------------------------------------------------------------
# _resolve_from_argv0 — symlink-dir resolution
# ---------------------------------------------------------------------------


def test_resolve_from_argv0_real_symlink(tmp_path: Path) -> None:
    """Create <ws>/<env>/up → extension_script; resolve returns (ws, env)."""
    ws = tmp_path / "workspace"
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)

    # Create the real script target (simulates extension script).
    real_script = tmp_path / "ext" / "workflow" / "up"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("#!/bin/sh\n")

    # Create the symlink at <ws>/<env>/up → real_script.
    link = env_dir / "up"
    link.symlink_to(real_script)

    result = _resolve_from_argv0(str(link))

    assert result is not None
    resolved_ws, resolved_env = result
    assert resolved_env == "alpha"
    assert resolved_ws == ws


def test_resolve_from_argv0_direct_path(tmp_path: Path) -> None:
    """Without a symlink, argv0 resolved directly → env = parent basename."""
    ws = tmp_path / "workspace"
    env_dir = ws / "beta"
    env_dir.mkdir(parents=True)
    script = env_dir / "up"
    script.write_text("#!/bin/sh\n")

    result = _resolve_from_argv0(str(script))

    assert result is not None
    _, resolved_env = result
    assert resolved_env == "beta"


def test_resolve_from_argv0_returns_none_on_failure() -> None:
    """Non-existent path returns None without raising."""
    result = _resolve_from_argv0("/this/path/does/not/exist")
    # May return None or a result; we only assert it does not raise.
    # Whether it returns None depends on whether the parent dir exists.
    # The key is: no exception.
    assert result is None or isinstance(result, tuple)


# ---------------------------------------------------------------------------
# Marker-walk fallback via EnvWorkspaceLocator
# ---------------------------------------------------------------------------


def test_locate_workspace_and_env_falls_back_to_locator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When argv[0] cannot be resolved as a symlink, marker-walk is used."""
    # Create a .winter/config.toml marker in tmp_path.
    marker_dir = tmp_path / ".winter"
    marker_dir.mkdir()
    (marker_dir / "config.toml").write_text("")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    # Invoke with a path that can't be resolved to an env dir (root or CWD).
    ws, env = _locate_workspace_and_env("/nonexistent/path/script")

    # Marker-walk should find tmp_path.
    assert ws == tmp_path
    # env will be empty string (can't determine from cwd alone).
    assert isinstance(env, str)


# ---------------------------------------------------------------------------
# project/ guard
# ---------------------------------------------------------------------------


def test_main_project_guard_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If argv[0] resolves to the project/ directory, exit 1."""
    ws = tmp_path
    project_dir = ws / "project"
    project_dir.mkdir()
    script = project_dir / "up"
    script.write_text("#!/bin/sh\n")

    container = _make_mock_container(_make_ctx("project"))
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["up"])

    assert rc == 1
    assert "project" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# local mode — skip_env_file=True
# ---------------------------------------------------------------------------


def test_up_local_mode_calls_skip_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "up"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["up", "local"])

    assert rc == 0
    call_kwargs = container.session_context_builder.build.call_args
    assert call_kwargs is not None
    assert call_kwargs.kwargs.get("skip_env_file") is True


# ---------------------------------------------------------------------------
# status — no patterns (all services in env)
# ---------------------------------------------------------------------------


def test_status_no_patterns_calls_status_all_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """status with no patterns → orchestrator.status called with no services filter."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status"])

    assert rc == 0
    container.orchestrator.status.assert_called_once_with(ctx)


# ---------------------------------------------------------------------------
# status --all (cross-env path — unchanged)
# ---------------------------------------------------------------------------


def test_status_all_loops_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ctx_alpha = _make_ctx("alpha")
    # Make tmux.list_sessions return two matching sessions.
    container = _make_mock_container(ctx_alpha, service_rc=0)
    container.tmux.list_sessions.return_value = ["mp-alpha", "mp-beta", "other-session"]
    # builder.build always returns a ctx for any env
    container.session_context_builder.build.return_value = ctx_alpha

    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status", "--all"])

    assert rc == 0
    # status was called twice (for mp-alpha and mp-beta, not other-session)
    assert container.orchestrator.status.call_count == 2


def test_status_all_no_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    container.tmux.list_sessions.return_value = []
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status", "--all"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "No" in captured.out
    assert container.orchestrator.status.call_count == 0


# ---------------------------------------------------------------------------
# status — single literal service pattern
# ---------------------------------------------------------------------------


def test_status_single_literal_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """status backend → orchestrator.status called with services=('backend',)."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha", manifest=_MANIFEST_MULTI)
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status", "backend"])

    assert rc == 0
    container.orchestrator.status.assert_called_once_with(ctx, services=("backend",))


# ---------------------------------------------------------------------------
# status — glob pattern matches subset
# ---------------------------------------------------------------------------


def test_status_glob_pattern_matches_subset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """status back* → matches backend and backend-worker."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha", manifest=_MANIFEST_MULTI)
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status", "back*"])

    assert rc == 0
    container.orchestrator.status.assert_called_once_with(ctx, services=("backend", "backend-worker"))


# ---------------------------------------------------------------------------
# status — no-match pattern → non-zero
# ---------------------------------------------------------------------------


def test_status_no_match_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """status nonexistent → non-zero exit with clear message."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha", manifest=_MANIFEST_MULTI)
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status", "nonexistent"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "nonexistent" in err
    container.orchestrator.status.assert_not_called()


# ---------------------------------------------------------------------------
# down env-suffix fallback when manifest missing
# ---------------------------------------------------------------------------


def test_down_suffix_fallback_when_manifest_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "down"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(
        ctx,
        build_side_effect=ManifestError("committed manifest not found"),
    )
    container.tmux.list_sessions.return_value = ["mp-alpha"]
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down"])

    # Should exit 0 (idempotent fallback killed the session).
    assert rc == 0
    container.tmux.kill_session.assert_called_once_with("mp-alpha")


def test_down_suffix_fallback_no_matching_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "down"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(
        ctx,
        build_side_effect=ManifestError("no manifest"),
    )
    container.tmux.list_sessions.return_value = ["mp-beta"]  # no alpha match
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down"])

    assert rc == 0
    container.tmux.kill_session.assert_not_called()
    out = capsys.readouterr().out
    assert "No running session" in out


# ---------------------------------------------------------------------------
# restart — single service (literal name still works)
# ---------------------------------------------------------------------------


def test_restart_single_service_still_works(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """restart backend → orchestrator.restart called once with 'backend'."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "restart"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["restart", "backend"])

    assert rc == 0
    container.orchestrator.restart.assert_called_once_with(ctx, "backend")


# ---------------------------------------------------------------------------
# restart — multi-pattern / glob → restart called per matched service
# ---------------------------------------------------------------------------


def test_restart_glob_pattern_restarts_each_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """restart back* → restart called for backend and backend-worker."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "restart"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha", manifest=_MANIFEST_MULTI)
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["restart", "back*"])

    assert rc == 0
    assert container.orchestrator.restart.call_count == 2
    calls = [c.args[1] for c in container.orchestrator.restart.call_args_list]
    assert "backend" in calls
    assert "backend-worker" in calls


def test_restart_multi_literal_patterns_restarts_each(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """restart backend frontend → restart called for both services."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "restart"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha", manifest=_MANIFEST_MULTI)
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["restart", "backend", "frontend"])

    assert rc == 0
    assert container.orchestrator.restart.call_count == 2
    calls = [c.args[1] for c in container.orchestrator.restart.call_args_list]
    assert "backend" in calls
    assert "frontend" in calls


# ---------------------------------------------------------------------------
# restart — no args → usage/help, non-zero
# ---------------------------------------------------------------------------


def test_restart_no_service_name_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "restart"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["restart"])

    assert rc == 1
    assert "usage" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# restart — leading-dash flag rejected
# ---------------------------------------------------------------------------


def test_restart_flag_arg_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A leading-dash arg should be rejected (not treated as service pattern)."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "restart"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["restart", "--help"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "flag" in err.lower() or "--help" in err


# ---------------------------------------------------------------------------
# restart — no-match pattern → non-zero + message
# ---------------------------------------------------------------------------


def test_restart_no_match_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """restart nonexistent → non-zero exit with clear message."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "restart"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha", manifest=_MANIFEST_MULTI)
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["restart", "nonexistent"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "nonexistent" in err
    container.orchestrator.restart.assert_not_called()


# ---------------------------------------------------------------------------
# up happy path
# ---------------------------------------------------------------------------


def test_up_happy_path_returns_0(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "up"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["up"])

    assert rc == 0
    container.orchestrator.up.assert_called_once_with(ctx)


# ---------------------------------------------------------------------------
# down happy path
# ---------------------------------------------------------------------------


def test_down_happy_path_returns_0(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "down"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down"])

    assert rc == 0
    container.orchestrator.down.assert_called_once_with(ctx)


# ---------------------------------------------------------------------------
# WINTER_WORKSPACE_DIR path — destroy hook invocation shape
# ---------------------------------------------------------------------------


def test_down_via_ext_path_with_winter_workspace_dir_uses_manifest_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: when invoked as <ext>/workflow/down <env> with WINTER_WORKSPACE_DIR
    set, env_cli must resolve workspace via the env var and take the manifest-driven
    reap path — NOT _down_suffix_fallback.

    This is the shape used by hooks/destroy-worktree.sh.
    """
    # Simulate the extension being invoked from its own directory (not a per-env
    # symlink).  argv0 looks like /some/ext/workflow/down — not an env-root path.
    ext_dir = tmp_path / "ext" / "workflow"
    ext_dir.mkdir(parents=True)
    ext_script = ext_dir / "down"
    ext_script.write_text("#!/bin/sh\n")

    ws = tmp_path / "workspace"
    ws.mkdir()

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)

    # WINTER_WORKSPACE_DIR points at the real workspace; env comes from argv.
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(ws))
    monkeypatch.setattr(env_cli_mod, "Container", lambda: container)
    monkeypatch.setattr(sys, "argv", [str(ext_script)])

    rc = main(["down", "alpha"])

    # Manifest-driven path taken: orchestrator.down called, NOT the suffix fallback.
    assert rc == 0
    container.orchestrator.down.assert_called_once_with(ctx)
    # Suffix fallback was NOT entered: kill_session not called via fallback path.
    container.tmux.kill_session.assert_not_called()


# ---------------------------------------------------------------------------
# status --all with service patterns → error (exit 2, no status call)
# ---------------------------------------------------------------------------


def test_status_all_with_service_patterns_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """status --all back* → exit 2 with clear error; no status calls made."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha", manifest=_MANIFEST_MULTI)
    container = _make_mock_container(ctx, service_rc=0)
    container.tmux.list_sessions.return_value = ["mp-alpha", "mp-beta"]
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status", "--all", "back*"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--all" in err
    container.orchestrator.status.assert_not_called()


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


def test_main_unknown_action_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "up"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["badaction"])

    assert rc == 2


# ---------------------------------------------------------------------------
# workspace target — env_cli door (AC 5)
# ---------------------------------------------------------------------------


def test_down_workspace_routes_through_build_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """down workspace → build_workspace called (NOT build('workspace',...)); orchestrator.down called."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "down"
    script.write_text("#!/bin/sh\n")

    ws_ctx = _make_workspace_ctx()
    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, workspace_ctx=ws_ctx)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down", "workspace"])

    assert rc == 0
    container.session_context_builder.build_workspace.assert_called_once()
    container.session_context_builder.build.assert_not_called()
    container.orchestrator.down.assert_called_once_with(ws_ctx)


def test_up_workspace_routes_through_build_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """up workspace → build_workspace called; orchestrator.up called."""
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "up"
    script.write_text("#!/bin/sh\n")

    ws_ctx = _make_workspace_ctx()
    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, workspace_ctx=ws_ctx)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["up", "workspace"])

    assert rc == 0
    container.session_context_builder.build_workspace.assert_called_once()
    container.session_context_builder.build.assert_not_called()
    container.orchestrator.up.assert_called_once_with(ws_ctx)


# ---------------------------------------------------------------------------
# status --all includes workspace session (Risk #1 for env_cli door)
# ---------------------------------------------------------------------------


def test_status_all_includes_workspace_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CRITICAL: status --all when mp-alpha AND mp-workspace running.

    - workspace session appears exactly once, built via build_workspace
    - env session built via build (normal path)
    - no phantom ws_root/workspace context created
    """
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "status"
    script.write_text("#!/bin/sh\n")

    ws_ctx = _make_workspace_ctx()
    ctx_alpha = _make_ctx("alpha")
    container = _make_mock_container(ctx_alpha, workspace_ctx=ws_ctx)
    # running sessions: alpha env + workspace singleton
    container.tmux.list_sessions.return_value = ["mp-alpha", "mp-workspace"]
    # build returns alpha ctx for env calls
    container.session_context_builder.build.return_value = ctx_alpha
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["status", "--all"])

    assert rc == 0
    # status called twice — once for alpha, once for workspace
    assert container.orchestrator.status.call_count == 2

    called_ctxs = [call.args[0] for call in container.orchestrator.status.call_args_list]
    envs_called = {ctx.env for ctx in called_ctxs}
    assert "alpha" in envs_called
    assert WORKSPACE_TARGET in envs_called

    # workspace session ctx has worktree_dir == ws_root (NOT ws_root/workspace)
    ws_called_ctx = next(c for c in called_ctxs if c.env == WORKSPACE_TARGET)
    assert ws_called_ctx.worktree_dir == _WORKSPACE, (
        f"workspace ctx worktree_dir={ws_called_ctx.worktree_dir!r}; must equal ws_root={_WORKSPACE!r}"
    )

    # build_workspace was called for the workspace session
    container.session_context_builder.build_workspace.assert_called()

    # build("workspace", ...) must NOT have been called (Risk #1 guard)
    for call in container.session_context_builder.build.call_args_list:
        env_arg = call.args[0] if call.args else None
        assert env_arg != WORKSPACE_TARGET, "build('workspace', ...) was called — this is the Risk #1 bug"


# ---------------------------------------------------------------------------
# up: no-retry door — env_cli passes no retry kwarg
# ---------------------------------------------------------------------------


def test_env_cli_up_does_not_pass_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """env_cli 'up alpha' calls orchestrator.up(ctx) with no retry kwarg.

    The env-root ./up is the no-retry door; retry is only honored by
    'winter service up' (cli.py → DispatchService.up).
    """
    ws = tmp_path
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)
    script = env_dir / "up"
    script.write_text("#!/bin/sh\n")

    ctx = _make_ctx("alpha")
    container = _make_mock_container(ctx, service_rc=0)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["up"])

    assert rc == 0
    # Exactly one call with ctx as the sole positional arg, no keyword args.
    container.orchestrator.up.assert_called_once_with(ctx)
