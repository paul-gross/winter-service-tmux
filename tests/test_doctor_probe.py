"""End-to-end tests for workflow/doctor.sh's "session-name collision" probe.

Mirrors the subprocess pattern established in tests/test_logwriter.py's
"End-to-end via subprocess" section and tests/test_cli.py's
`test_subprocess_valid_manifest` — the bash script itself has no automated
coverage, but the collision probe's classification logic is exercised
end-to-end via a faked `tmux` on PATH, per issue #35 AC4 (the workspace-scope
case must be covered so it does not regress).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from service_orchestrator.modules.orchestrate.tmux_server import BASELINE_PATH

_DOCTOR_SH = Path(__file__).parent.parent / "workflow" / "doctor.sh"

_MINIMAL_MANIFEST = """\
[[service]]
name = "shell"
target = "0.0"
"""

# A fake `tmux` answering only the invocations doctor.sh makes: `-V`
# (probe 1) and `-L <socket> ls -F '#{session_name}'` (probes 3 and 3b). The
# session lists are read from FAKE_TMUX_SESSIONS (the `winter` server) and
# FAKE_TMUX_DEFAULT_SESSIONS (the default server); an unset list means that
# server is not running. `-L winter show-environment -g` (probe 3c) prints
# FAKE_TMUX_SERVER_ENV. One shim serves every test case.
_TMUX_SHIM = """\
#!/usr/bin/env bash
if [[ "$1" == "-V" ]]; then
  echo "tmux 3.3a"
  exit 0
fi
[[ "$1" == "-L" ]] || exit 1
case "$2" in
  winter) var=FAKE_TMUX_SESSIONS ;;
  default) var=FAKE_TMUX_DEFAULT_SESSIONS ;;
  *) exit 1 ;;
esac
if [[ "$3" == "ls" && -n "${!var+set}" ]]; then
  printf '%s\\n' "${!var}"
  exit 0
fi
if [[ "$2" == "winter" && "$3" == "show-environment" && -n "${FAKE_TMUX_SERVER_ENV+set}" ]]; then
  printf '%s\\n' "$FAKE_TMUX_SERVER_ENV"
  exit 0
fi
exit 1
"""


def _make_workspace(tmp_path: Path, real_env: str) -> Path:
    """Build a workspace root containing one real feature-env worktree.

    The worktree marker doctor.sh looks for is a `.git` FILE (not directory)
    in an immediate child of `<workspace>/<real_env>/`.
    """
    workspace_dir = tmp_path / "workspace"
    child = workspace_dir / real_env / "some-repo"
    child.mkdir(parents=True)
    (child / ".git").write_text("gitdir: ../../.git/worktrees/some-repo\n")
    return workspace_dir


def _make_tmux_shim(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "tmux"
    shim.write_text(_TMUX_SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run_doctor(
    tmp_path: Path,
    *,
    sessions: str,
    default_sessions: str | None = None,
    server_env: str | None = None,
    real_env: str = "realenv",
    prefix: str = "zz",
    probe: str = "session-name collision",
) -> dict:
    """Run doctor.sh with a faked tmux + minimal manifest; return the parsed
    NDJSON object of *probe*."""
    workspace_dir = _make_workspace(tmp_path, real_env)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(_MINIMAL_MANIFEST)
    bin_dir = _make_tmux_shim(tmp_path)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "WINTER_WORKSPACE_DIR": str(workspace_dir),
        "WINTER_EXT_CONFIG_DIR": str(cfg_dir),
        "WINTER_SERVICE_PREFIX": prefix,
        "FAKE_TMUX_SESSIONS": sessions,
    }
    if default_sessions is not None:
        env["FAKE_TMUX_DEFAULT_SESSIONS"] = default_sessions
    if server_env is not None:
        env["FAKE_TMUX_SERVER_ENV"] = server_env
    result = subprocess.run(
        ["bash", str(_DOCTOR_SH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr

    for line in result.stdout.splitlines():
        obj = json.loads(line)
        if obj["name"] == probe:
            return obj
    raise AssertionError(f"no '{probe}' probe in output:\n{result.stdout}\n{result.stderr}")


def test_workspace_scope_session_is_own(tmp_path: Path) -> None:
    """A <prefix>-workspace session is classified as own, not a collision (#35)."""
    result = _run_doctor(tmp_path, sessions="zz-workspace")
    assert result["status"] == "pass", result


def test_feature_env_session_is_own(tmp_path: Path) -> None:
    """A <prefix>-<real-feature-env> session is still classified as own."""
    result = _run_doctor(tmp_path, sessions="zz-realenv")
    assert result["status"] == "pass", result


def test_foreign_session_still_warns(tmp_path: Path) -> None:
    """A genuinely foreign <prefix>-<suffix> session (neither `workspace` nor
    a real feature env) is still reported as a collision."""
    result = _run_doctor(tmp_path, sessions="zz-bogus")
    assert result["status"] == "warn", result
    assert "zz-bogus" in result["message"]


def test_default_server_sessions_ignore_the_winter_server(tmp_path: Path) -> None:
    """Own sessions on the `winter` server are where they belong — no warning."""
    result = _run_doctor(tmp_path, sessions="zz-realenv", probe="default-server sessions")
    assert result["status"] == "pass", result


def test_own_session_on_default_server_warns(tmp_path: Path) -> None:
    """An own session left on the default server is out of reach of ./down."""
    result = _run_doctor(
        tmp_path,
        sessions="",
        default_sessions="zz-realenv\nzz-workspace\nzz-bogus\nother-realenv",
        probe="default-server sessions",
    )
    assert result["status"] == "warn", result
    assert "zz-realenv zz-workspace" in result["message"]
    assert "zz-bogus" not in result["message"]
    assert "tmux -L default kill-session" in result["remediation"]


def test_clean_winter_server_passes(tmp_path: Path) -> None:
    result = _run_doctor(
        tmp_path,
        sessions="zz-realenv",
        server_env=f"HOME=/home/u\nPATH={BASELINE_PATH}\nPWD=/ws\n-DISPLAY",
        probe="winter server environment",
    )
    assert result["status"] == "pass", result


def test_dirty_winter_server_warns(tmp_path: Path) -> None:
    """A server started by hand from a feature env's shell carries its variables."""
    result = _run_doctor(
        tmp_path,
        sessions="zz-realenv",
        server_env="HOME=/home/u\nPATH=/ws/alpha/.venv/bin:/usr/bin\nWINTER_ENV=alpha",
        probe="winter server environment",
    )
    assert result["status"] == "warn", result
    assert "2 (PATH, WINTER_ENV)" in result["message"]


def test_no_winter_server_passes(tmp_path: Path) -> None:
    result = _run_doctor(tmp_path, sessions="", probe="winter server environment")
    assert result["status"] == "pass", result
