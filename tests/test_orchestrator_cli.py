"""Tests for service_orchestrator.cli — name-addressed winter entrypoint door.

Covers:
- All five actions accepted by ``main``
- ``logs`` refused non-zero with message
- ``restart`` reads ``WINTER_SERVICE_NAME``
- Missing env / unreadable manifest → non-zero with message containing env name
- Exit-code passthrough from the service
- Bad action → argparse-style non-zero
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import Service, ServiceManifest, Target
from service_orchestrator.cli import main
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.errors import OrchestratorError

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
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
        env_vars=None,
        env_file_path=None,
    )


def _patch_container(monkeypatch: pytest.MonkeyPatch, *, ctx: EnvContext, service_rc: int = 0) -> MagicMock:
    """Patch ``service_orchestrator.cli.Container`` with a fake that returns *ctx* and *service_rc*."""
    mock_builder = MagicMock()
    mock_builder.build.return_value = ctx

    mock_orchestrator = MagicMock()
    mock_orchestrator.up.return_value = service_rc
    mock_orchestrator.down.return_value = service_rc
    mock_orchestrator.status.return_value = service_rc
    mock_orchestrator.restart.return_value = service_rc

    mock_container = MagicMock()
    mock_container.env_context_builder = mock_builder
    mock_container.orchestrator = mock_orchestrator

    import service_orchestrator.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Container", lambda: mock_container)
    return mock_container


# ---------------------------------------------------------------------------
# Wrong argument count → exit 2
# ---------------------------------------------------------------------------


def test_main_no_args_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_main_one_arg_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["up"])
    assert rc == 2


def test_main_three_args_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["up", "alpha", "extra"])
    assert rc == 2


# ---------------------------------------------------------------------------
# Unknown action → exit 2
# ---------------------------------------------------------------------------


def test_main_bad_action_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["badaction", "alpha"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "badaction" in captured.err


# ---------------------------------------------------------------------------
# logs → refused non-zero with message
# ---------------------------------------------------------------------------


def test_main_logs_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["logs", "alpha"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "logs" in captured.err
    assert "unsupported" in captured.err.lower()


# ---------------------------------------------------------------------------
# up / down / status / restart — happy path, exit-code passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["up", "down", "status"])
def test_main_action_returns_0_on_success(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ctx("alpha")
    _patch_container(monkeypatch, ctx=ctx, service_rc=0)
    rc = main([action, "alpha"])
    assert rc == 0


@pytest.mark.parametrize("action", ["up", "down", "status"])
def test_main_action_passthrough_nonzero(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ctx("alpha")
    _patch_container(monkeypatch, ctx=ctx, service_rc=1)
    rc = main([action, "alpha"])
    assert rc == 1


# ---------------------------------------------------------------------------
# restart reads WINTER_SERVICE_NAME
# ---------------------------------------------------------------------------


def test_main_restart_reads_winter_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ctx("alpha")
    container = _patch_container(monkeypatch, ctx=ctx, service_rc=0)
    monkeypatch.setenv("WINTER_SERVICE_NAME", "backend")

    rc = main(["restart", "alpha"])

    assert rc == 0
    container.orchestrator.restart.assert_called_once_with(ctx, "backend")


def test_main_restart_missing_winter_service_name_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _make_ctx("alpha")
    _patch_container(monkeypatch, ctx=ctx, service_rc=0)
    monkeypatch.delenv("WINTER_SERVICE_NAME", raising=False)

    rc = main(["restart", "alpha"])

    assert rc == 1
    assert "WINTER_SERVICE_NAME" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Missing / unreadable manifest → non-zero with env name in message
# ---------------------------------------------------------------------------


def test_main_manifest_error_contains_env_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_builder = MagicMock()
    mock_builder.build.side_effect = ManifestError("committed manifest not found")
    mock_container = MagicMock()
    mock_container.env_context_builder = mock_builder

    import service_orchestrator.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Container", lambda: mock_container)

    rc = main(["up", "nonexistent-env"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "nonexistent-env" in err


def test_main_oserror_contains_env_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_builder = MagicMock()
    mock_builder.build.side_effect = OSError("env file not readable")
    mock_container = MagicMock()
    mock_container.env_context_builder = mock_builder

    import service_orchestrator.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Container", lambda: mock_container)

    rc = main(["up", "missing-env"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "missing-env" in err


def test_main_orchestrator_error_contains_env_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _make_ctx("delta")
    mock_builder = MagicMock()
    mock_builder.build.return_value = ctx

    mock_orchestrator = MagicMock()
    mock_orchestrator.up.side_effect = OrchestratorError("tmux failed")

    mock_container = MagicMock()
    mock_container.env_context_builder = mock_builder
    mock_container.orchestrator = mock_orchestrator

    import service_orchestrator.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Container", lambda: mock_container)

    rc = main(["up", "delta"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "delta" in err


# ---------------------------------------------------------------------------
# WINTER_WORKSPACE_DIR is passed to builder as workspace_root
# ---------------------------------------------------------------------------


def test_main_uses_winter_workspace_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ctx("alpha")
    container = _patch_container(monkeypatch, ctx=ctx, service_rc=0)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", "/custom/ws")

    main(["up", "alpha"])

    call_kwargs = container.env_context_builder.build.call_args
    assert call_kwargs is not None
    # workspace_root should be the Path from WINTER_WORKSPACE_DIR
    assert call_kwargs.kwargs.get("workspace_root") == Path("/custom/ws")


def test_main_no_winter_workspace_dir_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ctx("alpha")
    container = _patch_container(monkeypatch, ctx=ctx, service_rc=0)
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    main(["up", "alpha"])

    call_kwargs = container.env_context_builder.build.call_args
    assert call_kwargs.kwargs.get("workspace_root") is None
