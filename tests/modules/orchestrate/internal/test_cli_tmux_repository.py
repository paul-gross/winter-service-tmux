"""Tests for CliTmuxRepository.

All subprocess calls are patched at the adapter's import site.
Each test asserts:
  1. The exact tmux argv passed to subprocess.run.
  2. The adapter's return value / side effects.

Non-zero exit codes are tested to assert TmuxError is raised with the
correct structured fields.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from service_orchestrator.modules.orchestrate.errors import TmuxError
from service_orchestrator.modules.orchestrate.internal import cli_tmux_repository as adapter_module
from service_orchestrator.modules.orchestrate.internal.cli_tmux_repository import CliTmuxRepository
from service_orchestrator.modules.orchestrate.tmux_repository import PaneInfo


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "", args: list[str] | None = None):
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    mock.args = args or []
    return mock


# ---------------------------------------------------------------------------
# has_session
# ---------------------------------------------------------------------------


def test_has_session_returns_true_on_zero_exit(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0)
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    assert repo.has_session("my-session") is True
    fake_subprocess.run.assert_called_once_with(
        ["tmux", "has-session", "-t", "my-session"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_has_session_returns_false_on_nonzero_exit(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=1)
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    assert repo.has_session("no-session") is False


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_names(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0, stdout="mp-alpha\nmp-beta\n")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    result = repo.list_sessions()

    fake_subprocess.run.assert_called_once_with(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result == ["mp-alpha", "mp-beta"]


def test_list_sessions_returns_empty_on_no_server(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=1, stderr="no server running")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    assert repo.list_sessions() == []


# ---------------------------------------------------------------------------
# new_session
# ---------------------------------------------------------------------------


def test_new_session_passes_correct_argv(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0)
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    repo.new_session("mp-alpha", cwd=Path("/workspace/alpha"), width=200, height=50)

    fake_subprocess.run.assert_called_once_with(
        ["tmux", "new-session", "-d", "-s", "mp-alpha", "-c", "/workspace/alpha", "-x", "200", "-y", "50"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_new_session_raises_tmux_error_on_failure(monkeypatch):
    fake_subprocess = MagicMock()
    completed = _completed(returncode=1, stderr="duplicate session", args=["tmux", "new-session"])
    fake_subprocess.run.return_value = completed
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    with pytest.raises(TmuxError) as exc_info:
        repo.new_session("mp-alpha", cwd=Path("/workspace/alpha"), width=200, height=50)

    assert exc_info.value.exit_code == 1
    assert exc_info.value.stderr == "duplicate session"


# ---------------------------------------------------------------------------
# kill_session
# ---------------------------------------------------------------------------


def test_kill_session_passes_correct_argv(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0)
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    repo.kill_session("mp-alpha")

    fake_subprocess.run.assert_called_once_with(
        ["tmux", "kill-session", "-t", "mp-alpha"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_kill_session_tolerates_exit_1(monkeypatch):
    # exit 1 means "session not found" — mirrors `|| true` in bash
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=1)
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    repo.kill_session("mp-alpha")  # should not raise


# ---------------------------------------------------------------------------
# list_windows
# ---------------------------------------------------------------------------


def test_list_windows_passes_correct_argv(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0, stdout="0\n1\n")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    result = repo.list_windows("mp-alpha")

    fake_subprocess.run.assert_called_once_with(
        ["tmux", "list-windows", "-t", "mp-alpha", "-F", "#{window_index}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result == ["0", "1"]


def test_list_windows_raises_on_failure(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=1, stderr="no session", args=["tmux", "list-windows"])
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    with pytest.raises(TmuxError):
        repo.list_windows("mp-alpha")


# ---------------------------------------------------------------------------
# list_panes — the critical format test
# ---------------------------------------------------------------------------


def test_list_panes_passes_correct_argv(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0, stdout="0.0 12345\n0.1 12346\n1.0 12347\n")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    result = repo.list_panes("mp-alpha")

    fake_subprocess.run.assert_called_once_with(
        ["tmux", "list-panes", "-s", "-t", "mp-alpha", "-F", "#{window_index}.#{pane_index} #{pane_pid}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result == [
        PaneInfo(target="0.0", pid=12345),
        PaneInfo(target="0.1", pid=12346),
        PaneInfo(target="1.0", pid=12347),
    ]


def test_list_panes_parses_pane_info_correctly(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0, stdout="2.3 99999\n")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    result = repo.list_panes("mp-alpha")

    assert len(result) == 1
    assert result[0].target == "2.3"
    assert result[0].pid == 99999


def test_list_panes_raises_on_failure(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=1, stderr="no session", args=["tmux", "list-panes"])
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    with pytest.raises(TmuxError):
        repo.list_panes("mp-alpha")


# ---------------------------------------------------------------------------
# send_keys
# ---------------------------------------------------------------------------


def test_send_keys_passes_correct_argv(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0)
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    repo.send_keys("mp-alpha", "0.1", "echo hello")

    fake_subprocess.run.assert_called_once_with(
        ["tmux", "send-keys", "-t", "mp-alpha:0.1", "echo hello", "Enter"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_send_keys_raises_on_failure(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=1, stderr="bad target", args=["tmux", "send-keys"])
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    with pytest.raises(TmuxError):
        repo.send_keys("mp-alpha", "0.0", "some command")


# ---------------------------------------------------------------------------
# capture_pane
# ---------------------------------------------------------------------------


def test_capture_pane_passes_correct_argv(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=0, stdout="some output\n")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    result = repo.capture_pane("mp-alpha", "0.0")

    fake_subprocess.run.assert_called_once_with(
        ["tmux", "capture-pane", "-t", "mp-alpha:0.0", "-p"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result == "some output\n"


def test_capture_pane_raises_on_failure(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _completed(returncode=1, stderr="no pane", args=["tmux", "capture-pane"])
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    repo = CliTmuxRepository()
    with pytest.raises(TmuxError):
        repo.capture_pane("mp-alpha", "0.0")
