"""Tests for service_orchestrator.modules.orchestrate.status_report.

Covers:
- ``build_launch_line`` exact-string assertions including empty-command
  banner-only path.
- ``last_non_blank_line`` extraction.
- ``truncate_status_line`` 80-col truncation.
"""

from __future__ import annotations

from pathlib import Path

from service_orchestrator.modules.orchestrate.status_report import (
    build_launch_line,
    last_non_blank_line,
    truncate_status_line,
)

_WORKTREE = Path("/workspace/alpha")
_ENV_FILE = Path("/workspace/alpha/.winter.env")


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
