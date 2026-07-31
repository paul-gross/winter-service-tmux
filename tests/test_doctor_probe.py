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

_DOCTOR_SH = Path(__file__).parent.parent / "workflow" / "doctor.sh"

_MINIMAL_MANIFEST = """\
[[service]]
name = "shell"
target = "0.0"
"""

# A fake `tmux` answering only the two invocations doctor.sh makes:
# `-V` (probe 1) and `ls -F '#{session_name}'` (probe 3). The session list
# is read from FAKE_TMUX_SESSIONS so one shim serves every test case.
_TMUX_SHIM = """\
#!/usr/bin/env bash
if [[ "$1" == "-V" ]]; then
  echo "tmux 3.3a"
  exit 0
fi
if [[ "$1" == "ls" ]]; then
  printf '%s\\n' "$FAKE_TMUX_SESSIONS"
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


def _run_doctor(tmp_path: Path, *, sessions: str, real_env: str = "realenv", prefix: str = "zz") -> dict:
    """Run doctor.sh with a faked tmux + minimal manifest; return the parsed
    "session-name collision" NDJSON object."""
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
    result = subprocess.run(
        ["bash", str(_DOCTOR_SH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr

    for line in result.stdout.splitlines():
        obj = json.loads(line)
        if obj["name"] == "session-name collision":
            return obj
    raise AssertionError(f"no 'session-name collision' probe in output:\n{result.stdout}\n{result.stderr}")


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
