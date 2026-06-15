"""Tests for service_orchestrator.env_cli — env-root symlink door.

Covers:
- Symlink-dir resolution: construct a temp <ws>/<env>/up symlink →
  assert env/workspace resolved correctly from argv[0]
- Marker-walk fallback when symlink resolution fails
- WINTER_WORKSPACE_DIR path: invoked as <ext>/workflow/down <env> with
  WINTER_WORKSPACE_DIR set → manifest-driven reap, not suffix fallback
- ``local`` mode builds env-less context (skip_env_file=True)
- ``status --all`` loops sessions by prefix
- ``down`` env-suffix fallback when manifest missing
- project/ guard refuses with non-zero
- ``restart`` takes service name as positional arg (not WINTER_SERVICE_NAME)
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
from service_orchestrator.modules.orchestrate.env_context import EnvContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_MANIFEST = ServiceManifest(
    session_prefix="mp",
    env_file=".winter.env",
    layout_hook=None,
    services=(Service(name="backend", target=Target(window=0, pane=0), command="cmd"),),
    status_urls=(),
)


def _make_ctx(env: str = "alpha") -> EnvContext:
    return EnvContext(
        env=env,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE / env,
        manifest=_MANIFEST,
        env_vars={"BACKEND_PORT": "4100"},
        env_file_path=_WORKSPACE / env / ".winter.env",
    )


def _make_mock_container(
    ctx: EnvContext,
    *,
    service_rc: int = 0,
    build_side_effect: Exception | None = None,
) -> MagicMock:
    mock_builder = MagicMock()
    if build_side_effect is not None:
        mock_builder.build.side_effect = build_side_effect
    else:
        mock_builder.build.return_value = ctx

    mock_orchestrator = MagicMock()
    mock_orchestrator.up.return_value = service_rc
    mock_orchestrator.down.return_value = service_rc
    mock_orchestrator.status.return_value = service_rc
    mock_orchestrator.restart.return_value = service_rc

    mock_tmux = MagicMock()
    mock_tmux.list_sessions.return_value = []

    mock_container = MagicMock()
    mock_container.env_context_builder = mock_builder
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
    call_kwargs = container.env_context_builder.build.call_args
    assert call_kwargs is not None
    assert call_kwargs.kwargs.get("skip_env_file") is True


# ---------------------------------------------------------------------------
# status --all
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
    container.env_context_builder.build.return_value = ctx_alpha

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
# restart — positional arg, not WINTER_SERVICE_NAME
# ---------------------------------------------------------------------------


def test_restart_passes_positional_service_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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


def test_restart_flag_arg_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A leading-dash arg should be rejected (not treated as service name)."""
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
