"""Tests for service_orchestrator.modules.orchestrate.status_report.

Covers:
- ``build_launch_line`` exact-string assertions including empty-command
  banner-only path.
- ``last_non_blank_line`` extraction.
- ``truncate_status_line`` 80-col truncation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service_orchestrator.modules.orchestrate.status_report import (
    build_launch_line,
    last_non_blank_line,
    logwriter_path,
    truncate_status_line,
)

_WORKTREE = Path("/workspace/alpha")
_ENV_FILE = Path("/workspace/alpha/.winter.env")


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


def test_build_launch_line_with_env_file_and_command() -> None:
    line = build_launch_line(_WORKTREE, _ENV_FILE, "backend", "npm run start:dev")
    expected = f"cd '{_WORKTREE}' && source '{_ENV_FILE}' && echo '=== backend ===' && npm run start:dev"
    assert line == expected


def test_build_launch_line_without_env_file() -> None:
    line = build_launch_line(_WORKTREE, None, "backend", "npm run start:dev")
    expected = f"cd '{_WORKTREE}' && echo '=== backend ===' && npm run start:dev"
    assert line == expected


def test_build_launch_line_empty_command_banner_only_with_env_file() -> None:
    """Empty command → banner only; no trailing '&& <cmd>'."""
    line = build_launch_line(_WORKTREE, _ENV_FILE, "shell", "")
    expected = f"cd '{_WORKTREE}' && source '{_ENV_FILE}' && echo '=== shell ==='"
    assert line == expected
    assert line.endswith("=== shell ==='")


def test_build_launch_line_empty_command_banner_only_without_env_file() -> None:
    line = build_launch_line(_WORKTREE, None, "shell", "")
    expected = f"cd '{_WORKTREE}' && echo '=== shell ==='"
    assert line == expected


def test_build_launch_line_command_with_spaces() -> None:
    line = build_launch_line(_WORKTREE, None, "worker", "python -m worker --reload")
    expected = f"cd '{_WORKTREE}' && echo '=== worker ===' && python -m worker --reload"
    assert line == expected


def test_build_launch_line_path_with_spaces() -> None:
    wt = Path("/my workspace/alpha")
    ef = Path("/my workspace/alpha/.winter.env")
    line = build_launch_line(wt, ef, "svc", "cmd")
    assert f"cd '{wt}'" in line
    assert f"source '{ef}'" in line


def test_build_launch_line_cd_is_first_segment() -> None:
    line = build_launch_line(_WORKTREE, _ENV_FILE, "svc", "echo hi")
    assert line.startswith(f"cd '{_WORKTREE}'")


def test_build_launch_line_source_before_banner() -> None:
    line = build_launch_line(_WORKTREE, _ENV_FILE, "svc", "cmd")
    parts = line.split(" && ")
    assert parts[0].startswith("cd ")
    assert parts[1].startswith("source ")
    assert "echo '=== svc ==='" in parts[2]


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
        f"cd '{_WORKTREE}' && echo '=== svc ===' && "
        f"{{ npm run start ; }} 2>&1 | "
        f"python3 '{writer}' '{logfile}' "
        f"--rotate-size 10485760 --max-rotations 5"
    )
    assert line == expected


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
    assert line == f"cd '{_WORKTREE}' && echo '=== shell ==='"
    assert "python3" not in line


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
