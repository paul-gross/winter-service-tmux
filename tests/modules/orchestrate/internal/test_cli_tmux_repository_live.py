"""Real-tmux checks for the clean-server guarantee, on a throwaway socket.

The argv-level tests in ``test_cli_tmux_repository.py`` cannot show that tmux
itself keeps a dirty caller's variables out of the server and its panes, or
that a hook's bare ``tmux`` reaches the server through ``tmux_env_value``.
Skipped when tmux is not installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from service_orchestrator.modules.orchestrate.internal.cli_tmux_repository import CliTmuxRepository
from service_orchestrator.modules.orchestrate.tmux_server import BASELINE_PATH, leaked_names

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def socket_name() -> Iterator[str]:
    name = f"winter-test-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, check=False)
    # kill-server leaves the socket file behind.
    (Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / name).unlink(missing_ok=True)


def _show_environment(socket_name: str, *target: str) -> dict[str, str]:
    out = subprocess.run(
        ["tmux", "-L", socket_name, "show-environment", *target], capture_output=True, text=True, check=True
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_dirty_callers_never_reach_the_server_or_its_sessions(
    socket_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WINTER_LEAK_PROBE", "from-alpha")
    # An update-environment variable outside the allow-list: tmux would copy it
    # into the second session's environment if new_session passed it through.
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 5000 10.0.0.2 22")
    monkeypatch.setenv("PATH", f"/ws/alpha/.venv/bin{os.pathsep}{os.environ['PATH']}")
    repo = CliTmuxRepository(socket_name=socket_name)

    repo.new_session("first", cwd=tmp_path, width=80, height=24)
    repo.new_session("second", cwd=tmp_path, width=80, height=24)

    server_env = _show_environment(socket_name, "-g")
    assert "WINTER_LEAK_PROBE" not in server_env
    assert server_env["PATH"] == BASELINE_PATH
    assert "SSH_CONNECTION" not in _show_environment(socket_name, "-t", "second")


def test_doctor_sees_a_clean_server_as_clean(socket_name: str, tmp_path: Path) -> None:
    """Whatever tmux or the platform adds itself must not trip the doctor's leak check."""
    CliTmuxRepository(socket_name=socket_name).new_session("clean", cwd=tmp_path, width=80, height=24)

    assert leaked_names(_show_environment(socket_name, "-g")) == []


def test_tmux_env_value_routes_a_bare_tmux_to_the_session(socket_name: str, tmp_path: Path) -> None:
    repo = CliTmuxRepository(socket_name=socket_name)
    repo.new_session("hooked", cwd=tmp_path, width=80, height=24)

    hook_env = {**os.environ, "TMUX": repo.tmux_env_value("hooked")}
    hook_env.pop("TMUX_PANE", None)
    subprocess.run(["tmux", "split-window", "-t", "hooked:0.0"], env=hook_env, check=True)

    assert [pane.target for pane in repo.list_panes("hooked")] == ["0.0", "0.1"]
