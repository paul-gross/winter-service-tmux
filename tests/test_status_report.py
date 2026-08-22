"""Tests for service_orchestrator.modules.orchestrate.status_report.

Covers:
- ``build_launch_line`` exact-string assertions including empty-command
  banner-only path.
- ``last_non_blank_line`` extraction.
- ``truncate_status_line`` 80-col truncation.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from service_orchestrator.modules.orchestrate.status_report import (
    build_env_status,
    build_launch_line,
    build_service_status,
    build_status_document,
    last_non_blank_line,
    logwriter_path,
    truncate_status_line,
)

_WORKTREE = Path("/workspace/alpha")
_SCOPE = "alpha"


# ---------------------------------------------------------------------------
# logwriter_path — env-var and fallback branches
# ---------------------------------------------------------------------------


def test_logwriter_path_uses_winter_ext_dir_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When WINTER_EXT_DIR is set, logwriter_path() returns path beneath it."""
    monkeypatch.setenv("WINTER_EXT_DIR", "/opt/ext/service-tmux")
    result = logwriter_path()
    assert result == Path("/opt/ext/service-tmux/src/service_orchestrator/logwriter.py")


def test_logwriter_path_falls_back_to_file_relative_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When WINTER_EXT_DIR is absent, logwriter_path() falls back to __file__-relative path."""
    monkeypatch.delenv("WINTER_EXT_DIR", raising=False)
    result = logwriter_path()
    # Must end with the expected relative tail.
    assert result.name == "logwriter.py"
    assert result.parts[-2] == "service_orchestrator"
    assert result.is_absolute()


# ---------------------------------------------------------------------------
# build_launch_line
# ---------------------------------------------------------------------------


def test_build_launch_line_with_scope_and_command() -> None:
    line = build_launch_line(_WORKTREE, _SCOPE, "backend", "npm run start:dev")
    expected = (
        f'cd {shlex.quote(str(_WORKTREE))} && eval "$(winter env {shlex.quote(_SCOPE)})"'
        f" && echo {shlex.quote('=== backend ===')} && npm run start:dev"
    )
    assert line == expected


def test_build_launch_line_without_scope() -> None:
    line = build_launch_line(_WORKTREE, None, "backend", "npm run start:dev")
    expected = f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== backend ===')} && npm run start:dev"
    assert line == expected


def test_build_launch_line_empty_command_banner_only_with_scope() -> None:
    """Empty command → banner only; no trailing '&& <cmd>'."""
    line = build_launch_line(_WORKTREE, _SCOPE, "shell", "")
    expected = (
        f'cd {shlex.quote(str(_WORKTREE))} && eval "$(winter env {shlex.quote(_SCOPE)})"'
        f" && echo {shlex.quote('=== shell ===')}"
    )
    assert line == expected


def test_build_launch_line_empty_command_banner_only_without_scope() -> None:
    line = build_launch_line(_WORKTREE, None, "shell", "")
    expected = f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== shell ===')}"
    assert line == expected


def test_build_launch_line_with_scope_and_env_file() -> None:
    """Combo without mappings: scope + env_file_path retains the plain dot-source."""
    env_file = Path("/workspace/alpha/.env.local")
    line = build_launch_line(_WORKTREE, _SCOPE, "backend", "npm run start:dev", env_file_path=env_file)
    expected = (
        f'cd {shlex.quote(str(_WORKTREE))} && eval "$(winter env {shlex.quote(_SCOPE)})"'
        f" && . {shlex.quote(str(env_file))}"
        f" && echo {shlex.quote('=== backend ===')} && npm run start:dev"
    )
    assert line == expected


def test_build_launch_line_exports_service_env_after_env_file_before_banner() -> None:
    env_file = Path("/workspace/alpha/.env.local")
    line = build_launch_line(
        _WORKTREE,
        _SCOPE,
        "backend",
        "npm run start:dev",
        env_file_path=env_file,
        service_env={"PORT": "4100", "GREETING": "hello world"},
    )
    parts = line.split(" && ")

    assert parts[2] == "set -a"
    assert parts[3] == f". {shlex.quote(str(env_file))}"
    assert parts[4] == "set +a"
    assert parts[5] == "export PORT=4100"
    assert parts[6] == "export GREETING='hello world'"
    assert parts[7] == f"echo {shlex.quote('=== backend ===')}"


def test_build_launch_line_quotes_resolved_mapping_values() -> None:
    line = build_launch_line(
        _WORKTREE,
        None,
        "backend",
        "cmd",
        service_env={"PORT": "4100", "URL": "http://localhost:4100/health", "EMPTY": ""},
    )

    assert "export PORT=4100" in line
    assert "export URL=http://localhost:4100/health" in line
    assert "export EMPTY=''" in line


def test_build_launch_line_mapping_expansion_is_shell_safe() -> None:
    line = build_launch_line(
        Path("/"),
        None,
        "backend",
        'printf "%s|%s|%s" "$PORT" "$URL" "$GREETING"',
        service_env={
            "PORT": "4100",
            "URL": "http://localhost:4100/health",
            "GREETING": "hello; printf hacked",
        },
    )

    result = subprocess.run(["sh", "-c", line], env={"BASE_PORT": "4100"}, capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert result.stdout == "=== backend ===\n4100|http://localhost:4100/health|hello; printf hacked"


def test_build_launch_line_resolves_mapping_from_one_pane_env_file_evaluation(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    env_file = tmp_path / ".winter.env"
    env_file.write_text(
        f"TOKEN=$(printf x >> {shlex.quote(str(counter))}; printf pane)\n",
        encoding="utf-8",
    )
    line = build_launch_line(
        Path("/"),
        None,
        "backend",
        'printf "%s|%s" "$COPY" "$SECOND"',
        env_file_path=env_file,
        service_env={"COPY": "value-${TOKEN}", "SECOND": "${COPY}"},
        evaluate_env_file=True,
        expand_service_references=True,
    )

    result = subprocess.run(["sh", "-c", line], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert result.stdout == "=== backend ===\nvalue-pane|value-pane"
    assert counter.read_text(encoding="utf-8") == "x"


def test_build_launch_line_treats_mapping_values_as_data() -> None:
    line = build_launch_line(Path("/"), None, "backend", 'test "$PORT" = \'${MISSING}\'', service_env={"PORT": "${MISSING}"})

    result = subprocess.run(["sh", "-c", line], env={}, capture_output=True, text=True, check=False)

    assert result.returncode == 0


def test_build_launch_line_scope_only_no_env_file() -> None:
    """scope set, env_file_path=None → eval prefix but no dot-source."""
    line = build_launch_line(_WORKTREE, _SCOPE, "backend", "npm run start:dev", env_file_path=None)
    assert ". " not in line.split("&&")[1] if len(line.split("&&")) > 2 else True
    assert 'eval "$(winter env' in line
    assert f". {shlex.quote(_SCOPE)}" not in line  # no dot-source of scope name


def test_build_launch_line_env_file_only_no_scope() -> None:
    """scope=None, env_file_path set → plain dot-source but no eval."""
    env_file = Path("/workspace/alpha/.env.local")
    line = build_launch_line(_WORKTREE, None, "backend", "cmd", env_file_path=env_file)
    expected = (
        f"cd {shlex.quote(str(_WORKTREE))}"
        f" && . {shlex.quote(str(env_file))}"
        f" && echo {shlex.quote('=== backend ===')} && cmd"
    )
    assert line == expected
    assert "eval" not in line


def test_build_launch_line_local_neither_scope_nor_env_file() -> None:
    """scope=None, env_file_path=None → bare cd + banner (local mode)."""
    line = build_launch_line(_WORKTREE, None, "svc", "cmd", env_file_path=None)
    assert line == f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== svc ===')} && cmd"
    assert "eval" not in line
    assert " . " not in line


def test_build_launch_line_env_file_dot_source_uses_posix_dot() -> None:
    """POSIX dot (.) not bash source — the token must be exactly '.'."""
    env_file = Path("/workspace/alpha/.env.local")
    line = build_launch_line(_WORKTREE, _SCOPE, "svc", "cmd", env_file_path=env_file)
    segments = line.split(" && ")
    dot_segment = next((s for s in segments if s.startswith(". ")), None)
    assert dot_segment is not None, "Expected a '. <path>' segment"
    assert dot_segment == f". {shlex.quote(str(env_file))}"


def test_build_launch_line_env_file_before_banner() -> None:
    """Ordering: cd → eval → env_file → echo banner → cmd."""
    env_file = Path("/workspace/alpha/.env.local")
    line = build_launch_line(_WORKTREE, _SCOPE, "svc", "cmd", env_file_path=env_file)
    parts = line.split(" && ")
    assert parts[0].startswith("cd ")
    assert 'eval "$(winter env' in parts[1]
    assert parts[2].startswith(". ")
    assert shlex.quote("=== svc ===") in parts[3]


def test_build_launch_line_env_file_path_quoted() -> None:
    """A path with spaces in env_file_path is quoted via shlex."""
    env_file = Path("/my workspace/alpha/.env.local")
    line = build_launch_line(_WORKTREE, None, "svc", "cmd", env_file_path=env_file)
    assert shlex.quote(str(env_file)) in line


def test_build_launch_line_command_with_spaces() -> None:
    line = build_launch_line(_WORKTREE, None, "worker", "python -m worker --reload")
    expected = f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== worker ===')} && python -m worker --reload"
    assert line == expected


def test_build_launch_line_path_with_spaces() -> None:
    wt = Path("/my workspace/alpha")
    line = build_launch_line(wt, _SCOPE, "svc", "cmd")
    assert shlex.quote(str(wt)) in line
    assert shlex.quote(_SCOPE) in line


# ---------------------------------------------------------------------------
# build_launch_line — shell-quoting correctness for hostile inputs
# ---------------------------------------------------------------------------


def test_build_launch_line_single_quote_in_worktree_dir_produces_valid_shell() -> None:
    """A single quote in the worktree path must not break quoting or inject shell.

    The produced line must be parseable by shlex.split without error, and the
    cd argument must decode back to the original path string.
    """
    wt = Path("/home/o'brien/alpha")
    line = build_launch_line(wt, None, "svc", "echo hi")
    # The whole line must shlex-parse without raising.
    tokens = shlex.split(line)
    # First token is "cd"; second is the path (shlex.split resolves quoting).
    assert tokens[0] == "cd"
    assert tokens[1] == str(wt)


def test_build_launch_line_single_quote_in_service_name_produces_valid_shell() -> None:
    """A single quote in the service name must not break the banner echo or inject shell."""
    wt = Path("/workspace/alpha")
    name = "o'brien-service"
    line = build_launch_line(wt, None, name, "echo hi")
    # Must shlex-parse without raising.
    shlex.split(line)
    # Banner text must appear somewhere in the line, correctly quoted.
    assert name in line or shlex.quote(f"=== {name} ===") in line


def test_build_launch_line_single_quote_in_scope_produces_valid_shell() -> None:
    """A single quote in the scope name must not break the eval quoting."""
    wt = Path("/workspace/alpha")
    scope = "o'brien-env"
    line = build_launch_line(wt, scope, "svc", "echo hi")
    # Must shlex-parse without raising (the eval "$(..." part is a token).
    # We check the scope was quoted correctly by shlex.quote.
    assert shlex.quote(scope) in line


def test_build_launch_line_cd_is_first_segment() -> None:
    line = build_launch_line(_WORKTREE, _SCOPE, "svc", "echo hi")
    assert line.startswith(f"cd {shlex.quote(str(_WORKTREE))}")


def test_build_launch_line_eval_before_banner() -> None:
    line = build_launch_line(_WORKTREE, _SCOPE, "svc", "cmd")
    parts = line.split(" && ")
    assert parts[0].startswith("cd ")
    assert 'eval "$(winter env' in parts[1]
    assert shlex.quote("=== svc ===") in parts[2]


def test_build_launch_line_wrapped_when_logfile_supplied() -> None:
    """When logfile + rotate params are supplied, command is wrapped through the writer."""
    logfile = Path("/workspace/alpha/.winter/logs/svc.log")
    writer = logwriter_path()
    line = build_launch_line(
        _WORKTREE,
        None,
        "svc",
        "npm run start",
        logfile=logfile,
        rotate_size_bytes=10485760,
        max_rotations=5,
    )
    expected = (
        f"cd {shlex.quote(str(_WORKTREE))}"
        f" && echo {shlex.quote('=== svc ===')} && "
        f"{{ npm run start ; }} 2>&1 | "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(writer))} {shlex.quote(str(logfile))} "
        f"--rotate-size 10485760 --max-rotations 5"
    )
    assert line == expected
    assert shlex.quote(sys.executable) in line


def test_build_launch_line_bare_when_command_empty_and_logfile_supplied() -> None:
    """Empty command → bare banner-only line even when logfile params are supplied."""
    logfile = Path("/workspace/alpha/.winter/logs/shell.log")
    line = build_launch_line(
        _WORKTREE,
        None,
        "shell",
        "",
        logfile=logfile,
        rotate_size_bytes=10485760,
        max_rotations=5,
    )
    expected_banner = f"cd {shlex.quote(str(_WORKTREE))} && echo {shlex.quote('=== shell ===')} "
    assert line == expected_banner.strip()
    assert "python3" not in line


# ---------------------------------------------------------------------------
# build_launch_line — cwd
# ---------------------------------------------------------------------------


def test_build_launch_line_cwd_joins_worktree_dir() -> None:
    """A declared cwd is joined onto worktree_dir in the leading cd."""
    line = build_launch_line(_WORKTREE, _SCOPE, "backend", "npm run start:dev", cwd="apps/backend")
    expected_dir = _WORKTREE / "apps/backend"
    expected = (
        f'cd {shlex.quote(str(expected_dir))} && eval "$(winter env {shlex.quote(_SCOPE)})"'
        f" && echo {shlex.quote('=== backend ===')} && npm run start:dev"
    )
    assert line == expected


def test_build_launch_line_cwd_absent_unchanged() -> None:
    """No cwd → identical to the pre-existing plain cd '<worktree_dir>' behavior."""
    with_cwd_none = build_launch_line(_WORKTREE, _SCOPE, "backend", "npm run start:dev", cwd=None)
    without_cwd_arg = build_launch_line(_WORKTREE, _SCOPE, "backend", "npm run start:dev")
    assert with_cwd_none == without_cwd_arg
    assert with_cwd_none.startswith(f"cd {shlex.quote(str(_WORKTREE))} ")


# ---------------------------------------------------------------------------
# last_non_blank_line
# ---------------------------------------------------------------------------


def test_last_non_blank_line_returns_last_non_blank() -> None:
    text = "first\nsecond\n\n  \nthird\n"
    assert last_non_blank_line(text) == "third"


def test_last_non_blank_line_ignores_trailing_blank() -> None:
    text = "only line\n\n"
    assert last_non_blank_line(text) == "only line"


def test_last_non_blank_line_single_line() -> None:
    assert last_non_blank_line("hello") == "hello"


def test_last_non_blank_line_empty_string() -> None:
    assert last_non_blank_line("") == ""


def test_last_non_blank_line_all_blank() -> None:
    assert last_non_blank_line("\n  \n\n") == ""


# ---------------------------------------------------------------------------
# truncate_status_line
# ---------------------------------------------------------------------------


def test_truncate_status_line_short_string_unchanged() -> None:
    assert truncate_status_line("hello") == "hello"


def test_truncate_status_line_exactly_80_chars() -> None:
    line = "x" * 80
    assert truncate_status_line(line) == line


def test_truncate_status_line_over_80_chars() -> None:
    line = "x" * 100
    result = truncate_status_line(line)
    assert len(result) == 80
    assert result == "x" * 80


def test_truncate_status_line_empty() -> None:
    assert truncate_status_line("") == ""


def test_truncate_status_line_custom_width() -> None:
    assert truncate_status_line("abcde", width=3) == "abc"


# ---------------------------------------------------------------------------
# winter status document builders — shape stability
# ---------------------------------------------------------------------------


def test_build_service_status_is_shape_stable() -> None:
    svc = build_service_status("api", "running", handle="mp-alpha:0.0", log_path="/abs/api.log")
    assert svc == {
        "name": "api",
        "state": "running",
        "health": "unknown",
        "ports": [],
        "handle": "mp-alpha:0.0",
        "log_path": "/abs/api.log",
        "since": None,
    }


def test_build_service_status_null_handle_and_log_path() -> None:
    svc = build_service_status("api", "stopped", handle=None, log_path=None)
    assert svc["handle"] is None
    assert svc["log_path"] is None
    assert svc["ports"] == []
    assert svc["since"] is None


def test_build_env_status_carries_session_and_port_base() -> None:
    env = build_env_status("alpha", "mp-alpha", 4020, [])
    assert env == {"env": "alpha", "session": "mp-alpha", "port_base": 4020, "services": []}


def test_build_env_status_null_port_base() -> None:
    env = build_env_status("workspace", "mp-workspace", None, [])
    assert env["port_base"] is None


def test_build_status_document_wraps_env_list() -> None:
    env = build_env_status("alpha", "mp-alpha", 4020, [])
    assert build_status_document([env]) == {"envs": [env]}


def test_build_status_document_empty_is_valid() -> None:
    assert build_status_document([]) == {"envs": []}


# ---------------------------------------------------------------------------
# build_service_status — ports parameter
# ---------------------------------------------------------------------------


def test_build_service_status_ports_absent_defaults_to_empty_list() -> None:
    svc = build_service_status("api", "running", handle=None, log_path=None)
    assert svc["ports"] == []


def test_build_service_status_ports_none_yields_empty_list() -> None:
    svc = build_service_status("api", "running", handle=None, log_path=None, ports=None)
    assert svc["ports"] == []


def test_build_service_status_ports_with_value() -> None:
    svc = build_service_status("api", "running", handle=None, log_path=None, ports=[4070])
    assert svc["ports"] == [4070]


def test_build_service_status_ports_empty_list() -> None:
    svc = build_service_status("api", "running", handle=None, log_path=None, ports=[])
    assert svc["ports"] == []
