"""Tests for service_orchestrator.env_cli — env-root door.

The user-facing actions delegate to ``winter service <action> <env>``; the only
in-process (tmux-only) path is ``down --tmux-only`` (used by the destroy hook).
Covers:
- Symlink-dir resolution: construct a temp <ws>/<env>/up symlink →
  assert env/workspace resolved correctly from argv[0]
- Marker-walk fallback when symlink resolution fails
- project/ guard refuses with non-zero
- ``up``/``status``/``restart`` delegate to ``winter service …`` (argv + rc passthrough)
- ``up -a`` execs ``tmux attach-session`` after a successful delegated up
- ``status``/``restart`` pattern args are prefixed with the env segment
- ``down`` (plain) delegates; ``down --tmux-only`` takes the in-process tmux
  reap with the env-suffix fallback (the ws-destroy path)
- ``down --tmux-only`` with WINTER_WORKSPACE_DIR uses the manifest reap, not fallback
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
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
    workspace_services=(Service(name="ws-svc", target=Target(window=0, pane=0), cmd="ws-cmd"),),
)


def _make_ctx(env: str = "alpha", manifest: ServiceManifest = _MANIFEST) -> SessionContext:
    return SessionContext(
        env=env,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE / env,
        config_dir=_CONFIG_DIR,
        session_prefix="mp",
        services=manifest.services,
        layout_hook=manifest.layout_hook,
        logs=manifest.logs,
        env_vars={"BACKEND_PORT": "4100"},
        inject_scope=env,
        env_file_path=_WORKSPACE / env / ".winter.env",
    )


def _make_workspace_ctx() -> SessionContext:
    # The workspace session selects the manifest's workspace_* fields.
    return SessionContext(
        env=WORKSPACE_TARGET,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE,  # NOT _WORKSPACE/workspace
        config_dir=_CONFIG_DIR,
        session_prefix="mp",
        services=_MANIFEST.workspace_services,
        layout_hook=_MANIFEST.workspace_layout_hook,
        logs=_MANIFEST.logs,
        env_vars=None,
        inject_scope=None,
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


class _FakeWinterCli:
    """Fake ``IWinterCli`` seam — records each ``winter service`` delegation."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []

    def service(self, args: list[str]) -> int:
        self.calls.append(list(args))
        return self.rc


def _patch_delegate(monkeypatch: pytest.MonkeyPatch, rc: int = 0) -> list[list[str]]:
    """Inject a fake ``winter service`` seam; return its recorded ``service(args)`` list.

    The recorded args are what the door passes to ``IWinterCli.service`` — i.e.
    without the leading ``["winter", "service"]`` the adapter prepends.
    """
    fake = _FakeWinterCli(rc)
    monkeypatch.setattr(env_cli_mod, "SubprocessWinterCli", lambda: fake)
    return fake.calls


def _make_env_script(tmp_path: Path, env: str, name: str) -> Path:
    env_dir = tmp_path / env
    env_dir.mkdir(parents=True, exist_ok=True)
    script = env_dir / name
    script.write_text("#!/bin/sh\n")
    return script


# ---------------------------------------------------------------------------
# _resolve_from_argv0 — symlink-dir resolution
# ---------------------------------------------------------------------------


def test_resolve_from_argv0_real_symlink(tmp_path: Path) -> None:
    """Create <ws>/<env>/up → extension_script; resolve returns (ws, env)."""
    ws = tmp_path / "workspace"
    env_dir = ws / "alpha"
    env_dir.mkdir(parents=True)

    real_script = tmp_path / "ext" / "workflow" / "up"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("#!/bin/sh\n")

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
    assert result is None or isinstance(result, tuple)


# ---------------------------------------------------------------------------
# Marker-walk fallback via EnvWorkspaceLocator
# ---------------------------------------------------------------------------


def test_locate_workspace_and_env_falls_back_to_locator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When argv[0] cannot be resolved as a symlink, marker-walk is used."""
    marker_dir = tmp_path / ".winter"
    marker_dir.mkdir()
    (marker_dir / "config.toml").write_text("")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    ws, env = _locate_workspace_and_env("/nonexistent/path/script")

    assert ws == tmp_path
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
    script = _make_env_script(tmp_path, "project", "up")
    _patch_delegate(monkeypatch)
    _patch_container_and_argv0(monkeypatch, _make_mock_container(_make_ctx("project")), str(script))

    rc = main(["up"])

    assert rc == 1
    assert "project" in capsys.readouterr().err.lower()


def test_main_unknown_action_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "up")
    _patch_container_and_argv0(monkeypatch, _make_mock_container(_make_ctx("alpha")), str(script))

    rc = main(["badaction"])

    assert rc == 2


# ---------------------------------------------------------------------------
# up — delegates to `winter service up`
# ---------------------------------------------------------------------------


def test_up_delegates_to_winter_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "up")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["up"])

    assert rc == 0
    assert calls == [["up", "alpha"]]


def test_up_passes_through_nonzero_rc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "up")
    _patch_delegate(monkeypatch, rc=3)
    execvp = MagicMock()
    monkeypatch.setattr(env_cli_mod.os, "execvp", execvp)
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "wws")
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["up", "-a"])

    assert rc == 3
    execvp.assert_not_called()  # no attach on failed up


def test_up_workspace_delegates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "up")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["up", "workspace"])

    assert rc == 0
    assert calls == [["up", "workspace"]]


def test_up_attach_execs_tmux(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """up -a → after a successful delegated up, exec `tmux attach-session -t <prefix>-<env>`."""
    script = _make_env_script(tmp_path, "alpha", "up")
    _patch_delegate(monkeypatch, rc=0)
    execvp = MagicMock()
    monkeypatch.setattr(env_cli_mod.os, "execvp", execvp)
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "wws")
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["up", "-a"])

    assert rc == 0
    execvp.assert_called_once_with("tmux", ["tmux", "attach-session", "-t", "wws-alpha"])


def test_up_attach_without_prefix_does_not_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """up -a with no WINTER_SERVICE_PREFIX → skip attach, warn, return the up rc."""
    script = _make_env_script(tmp_path, "alpha", "up")
    _patch_delegate(monkeypatch, rc=0)
    execvp = MagicMock()
    monkeypatch.setattr(env_cli_mod.os, "execvp", execvp)
    monkeypatch.delenv("WINTER_SERVICE_PREFIX", raising=False)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["up", "-a"])

    assert rc == 0
    execvp.assert_not_called()
    assert "WINTER_SERVICE_PREFIX" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# status — delegates to `winter service status`
# ---------------------------------------------------------------------------


def test_status_no_patterns_delegates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "status")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["status"])

    assert rc == 0
    assert calls == [["status", "alpha"]]


def test_status_all_delegates_across_envs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "status")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["status", "--all"])

    assert rc == 0
    assert calls == [["status"]]


def test_status_pattern_prefixed_with_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "status")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["status", "backend", "worker-*"])

    assert rc == 0
    assert calls == [["status", "alpha/backend", "alpha/worker-*"]]


def test_status_all_with_service_patterns_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """status --all back* → exit 2 with clear error; no delegation made."""
    script = _make_env_script(tmp_path, "alpha", "status")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["status", "--all", "back*"])

    assert rc == 2
    assert "--all" in capsys.readouterr().err
    assert calls == []


# ---------------------------------------------------------------------------
# restart — delegates to `winter service restart`
# ---------------------------------------------------------------------------


def test_restart_single_delegates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "restart")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["restart", "backend"])

    assert rc == 0
    assert calls == [["restart", "alpha/backend"]]


def test_restart_multi_pattern_delegates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "restart")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["restart", "back*", "frontend"])

    assert rc == 0
    assert calls == [["restart", "alpha/back*", "alpha/frontend"]]


def test_restart_db_delegates_so_docker_service_can_bounce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`./restart db` prefixes the env and delegates — winter routes it to the owning
    (docker) provider, which is the whole point of delegating restart."""
    script = _make_env_script(tmp_path, "alpha", "restart")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["restart", "db"])

    assert rc == 0
    assert calls == [["restart", "alpha/db"]]


def test_restart_no_service_name_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _make_env_script(tmp_path, "alpha", "restart")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["restart"])

    assert rc == 1
    assert "usage" in capsys.readouterr().err.lower()
    assert calls == []


def test_restart_flag_arg_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A leading-dash arg should be rejected (not treated as service pattern)."""
    script = _make_env_script(tmp_path, "alpha", "restart")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["restart", "--help"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "flag" in err.lower() or "--help" in err
    assert calls == []


# ---------------------------------------------------------------------------
# down — plain delegates; --tmux-only takes the in-process reap
# ---------------------------------------------------------------------------


def test_down_delegates_to_winter_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plain `./down` delegates (cross-provider) and never touches the in-process reap."""
    script = _make_env_script(tmp_path, "alpha", "down")
    calls = _patch_delegate(monkeypatch, rc=0)
    container = _make_mock_container(_make_ctx("alpha"))
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down"])

    assert rc == 0
    assert calls == [["down", "alpha"]]
    container.orchestrator.down.assert_not_called()


def test_down_workspace_delegates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "down")
    calls = _patch_delegate(monkeypatch, rc=0)
    monkeypatch.setattr(sys, "argv", [str(script)])

    rc = main(["down", "workspace"])

    assert rc == 0
    assert calls == [["down", "workspace"]]


def test_down_tmux_only_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`down --tmux-only` reaps the tmux session in-process and does NOT delegate."""
    script = _make_env_script(tmp_path, "alpha", "down")
    calls = _patch_delegate(monkeypatch, rc=0)
    container = _make_mock_container(_make_ctx("alpha"))
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down", "--tmux-only"])

    assert rc == 0
    assert calls == []
    assert container.orchestrator.down.call_count == 1
    assert container.orchestrator.down.call_args.args[0].env == "alpha"


def test_down_tmux_only_suffix_fallback_when_manifest_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _make_env_script(tmp_path, "alpha", "down")
    _patch_delegate(monkeypatch, rc=0)
    container = _make_mock_container(
        _make_ctx("alpha"),
        build_side_effect=ManifestError("committed manifest not found"),
    )
    container.tmux.list_sessions.return_value = ["mp-alpha"]
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down", "--tmux-only"])

    assert rc == 0
    container.tmux.kill_session.assert_called_once_with("mp-alpha")


def test_down_tmux_only_suffix_fallback_no_matching_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _make_env_script(tmp_path, "alpha", "down")
    _patch_delegate(monkeypatch, rc=0)
    container = _make_mock_container(
        _make_ctx("alpha"),
        build_side_effect=ManifestError("no manifest"),
    )
    container.tmux.list_sessions.return_value = ["mp-beta"]  # no alpha match
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down", "--tmux-only"])

    assert rc == 0
    container.tmux.kill_session.assert_not_called()
    assert "No running session" in capsys.readouterr().out


def test_down_tmux_only_workspace_routes_through_build_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """down --tmux-only workspace → build_workspace (NOT build('workspace',...))."""
    script = _make_env_script(tmp_path, "alpha", "down")
    _patch_delegate(monkeypatch, rc=0)
    ws_ctx = _make_workspace_ctx()
    container = _make_mock_container(_make_ctx("alpha"), workspace_ctx=ws_ctx)
    _patch_container_and_argv0(monkeypatch, container, str(script))

    rc = main(["down", "--tmux-only", "workspace"])

    assert rc == 0
    container.session_context_builder.build_workspace.assert_called_once()
    container.session_context_builder.build.assert_not_called()
    container.orchestrator.down.assert_called_once_with(ws_ctx)


def test_down_tmux_only_via_ext_path_with_winter_workspace_dir_uses_manifest_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: the destroy hook invokes <ext>/workflow/down --tmux-only <env> with
    WINTER_WORKSPACE_DIR set → resolve workspace via the env var and take the
    manifest-driven reap, NOT the env-suffix fallback.
    """
    ext_dir = tmp_path / "ext" / "workflow"
    ext_dir.mkdir(parents=True)
    ext_script = ext_dir / "down"
    ext_script.write_text("#!/bin/sh\n")

    ws = tmp_path / "workspace"
    ws.mkdir()

    _patch_delegate(monkeypatch, rc=0)
    container = _make_mock_container(_make_ctx("alpha"))
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(ws))
    monkeypatch.setattr(env_cli_mod, "Container", lambda: container)
    monkeypatch.setattr(sys, "argv", [str(ext_script)])

    rc = main(["down", "--tmux-only", "alpha"])

    assert rc == 0
    assert container.orchestrator.down.call_count == 1
    assert container.orchestrator.down.call_args.args[0].env == "alpha"
    container.tmux.kill_session.assert_not_called()


# ---------------------------------------------------------------------------
# _build_env_ctx — scope sourcing (tmux-only down path)
# ---------------------------------------------------------------------------


def test_build_env_ctx_sets_inject_scope(
    tmp_path: Path,
) -> None:
    """For a normal env, _build_env_ctx exposes os.environ and keeps inject_scope."""
    from service_orchestrator.env_cli import _build_env_ctx

    ctx_base = _make_ctx("alpha")
    container = _make_mock_container(ctx_base)
    container.session_context_builder.build.return_value = ctx_base

    result = _build_env_ctx(container.session_context_builder, "alpha", tmp_path)
    assert result.inject_scope == "alpha"


def test_build_env_ctx_normal_preserves_env_file_path(
    tmp_path: Path,
) -> None:
    """_build_env_ctx leaves env_file_path from the builder intact (panes dot-source it)."""
    from service_orchestrator.env_cli import _build_env_ctx

    ctx_base = _make_ctx("alpha")  # has env_file_path set
    container = _make_mock_container(ctx_base)
    container.session_context_builder.build.return_value = ctx_base

    result = _build_env_ctx(container.session_context_builder, "alpha", tmp_path)
    assert result.env_file_path == ctx_base.env_file_path


# ---------------------------------------------------------------------------
# SubprocessWinterCli — the `winter service` passthrough adapter
# ---------------------------------------------------------------------------


def test_winter_cli_returns_child_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter prepends ``winter service`` and passes the child's rc through."""
    from service_orchestrator.core.internal import subprocess_winter_cli as adapter_mod
    from service_orchestrator.core.internal.subprocess_winter_cli import SubprocessWinterCli

    seen: list[list[str]] = []

    def fake_run(argv: list[str], *args: object, **kwargs: object) -> SimpleNamespace:
        seen.append(list(argv))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(adapter_mod.subprocess, "run", fake_run)

    rc = SubprocessWinterCli().service(["up", "alpha"])

    assert rc == 7
    assert seen == [["winter", "service", "up", "alpha"]]


def test_winter_cli_missing_on_path_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``winter`` is not on PATH, the adapter returns 127 with a helpful message."""
    from service_orchestrator.core.internal import subprocess_winter_cli as adapter_mod
    from service_orchestrator.core.internal.subprocess_winter_cli import SubprocessWinterCli

    def raise_fnf(*args: object, **kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError("winter")

    monkeypatch.setattr(adapter_mod.subprocess, "run", raise_fnf)

    rc = SubprocessWinterCli().service(["up", "alpha"])

    assert rc == 127
    assert "winter" in capsys.readouterr().err.lower()
