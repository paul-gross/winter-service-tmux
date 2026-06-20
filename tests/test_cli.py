"""Tests for service_manifest.cli — validate subcommand, exit codes, output modes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from service_manifest.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MANIFEST_SUBPATH = Path("ai") / "project"


def _write_manifest(tmp_path: Path, content: str) -> Path:
    """Write *content* to ai/project/setup-tmux.toml and return the file path."""
    d = tmp_path / _MANIFEST_SUBPATH
    d.mkdir(parents=True, exist_ok=True)
    p = d / "setup-tmux.toml"
    p.write_text(content, encoding="utf-8")
    return p


def _write_env(tmp_path: Path, content: str) -> Path:
    """Write *content* to .winter.env and return the file path."""
    p = tmp_path / ".winter.env"
    p.write_text(content, encoding="utf-8")
    return p


# A valid minimal manifest matching the example schema.
_VALID_MANIFEST = """\
session_prefix = "mp"
env_file = ".winter.env"

[[service]]
name = "backend"
target = "0.0"
command = "npm run start:dev"

[[service]]
name = "frontend"
target = "0.1"
command = "npm run dev"

[[service]]
name = "shell"
target = "1.0"
command = ""

[[status.url]]
label = "Backend"
url = "http://localhost:${BACKEND_PORT}"
"""

_VALID_ENV = "BACKEND_PORT=3000\n"


# ---------------------------------------------------------------------------
# In-process tests (call main() directly)
# ---------------------------------------------------------------------------


def test_valid_manifest_human_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_manifest(tmp_path, _VALID_MANIFEST)
    _write_env(tmp_path, _VALID_ENV)

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", str(tmp_path)])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "valid" in captured.out


def test_valid_manifest_json_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_manifest(tmp_path, _VALID_MANIFEST)
    _write_env(tmp_path, _VALID_ENV)

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", "--json", str(tmp_path)])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is True
    assert data["violations"] == []


def test_valid_manifest_no_env_file_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Manifest without env_file field + no status URL vars — still valid."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = "npm run start"
"""
    _write_manifest(tmp_path, content)

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", str(tmp_path)])

    assert exc_info.value.code == 0


def test_semantic_violation_duplicate_target_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two services on the same target → semantic violation → exit 1."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = "npm run start:dev"

[[service]]
name = "frontend"
target = "0.0"
command = "npm run dev"
"""
    _write_manifest(tmp_path, content)

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "0.0" in captured.out
    assert "backend" in captured.out
    assert "frontend" in captured.out


def test_semantic_violation_duplicate_target_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Duplicate target → --json output has ok=false and violation listed."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = "cmd"

[[service]]
name = "frontend"
target = "0.0"
command = "cmd"
"""
    _write_manifest(tmp_path, content)

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", "--json", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is False
    assert len(data["violations"]) >= 1
    assert any("0.0" in v for v in data["violations"])


def test_missing_manifest_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No setup-tmux.toml at all → ManifestError → exit 1."""
    with pytest.raises(SystemExit) as exc_info:
        main(["validate", str(tmp_path)])

    assert exc_info.value.code == 1


def test_missing_manifest_json_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No manifest → --json output has ok=false and a read-error message."""
    with pytest.raises(SystemExit) as exc_info:
        main(["validate", "--json", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is False
    assert len(data["violations"]) == 1
    assert "read error" in data["violations"][0]


def test_malformed_toml_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Malformed TOML → ManifestError → exit 1 with clear message to stderr."""
    _write_manifest(tmp_path, "session_prefix = [BROKEN\n")

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()


def test_malformed_toml_json_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Malformed TOML → --json output has ok=false and a read-error message."""
    _write_manifest(tmp_path, "session_prefix = [BROKEN\n")

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", "--json", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is False
    assert any("read error" in v for v in data["violations"])


def test_unresolvable_var_with_env_file_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Env file present but missing a required var → violation → exit 1."""
    content = """\
session_prefix = "mp"
env_file = ".winter.env"

[[service]]
name = "backend"
target = "0.0"
command = "cmd"

[[status.url]]
label = "Backend"
url = "http://localhost:${BACKEND_PORT}"
"""
    _write_manifest(tmp_path, content)
    _write_env(tmp_path, "OTHER_VAR=1234\n")  # BACKEND_PORT missing

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "BACKEND_PORT" in captured.out


def test_missing_env_file_skips_var_check_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """env_file declared but file absent → var checks skipped → valid."""
    content = """\
session_prefix = "mp"
env_file = ".winter.env"

[[service]]
name = "backend"
target = "0.0"
command = "cmd"

[[status.url]]
label = "Backend"
url = "http://localhost:${BACKEND_PORT}"
"""
    _write_manifest(tmp_path, content)
    # .winter.env intentionally NOT written

    with pytest.raises(SystemExit) as exc_info:
        main(["validate", str(tmp_path)])

    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Subprocess test — verify bare python3 + PYTHONPATH=src works (no uv)
# ---------------------------------------------------------------------------


def test_subprocess_valid_manifest(tmp_path: Path) -> None:
    """Verify the CLI runs with bare python3 + PYTHONPATH=src (stdlib-only)."""
    _write_manifest(tmp_path, _VALID_MANIFEST)
    _write_env(tmp_path, _VALID_ENV)

    src_dir = Path(__file__).parent.parent / "src"
    result = subprocess.run(
        [sys.executable, "-m", "service_manifest.cli", "validate", str(tmp_path)],
        env={**__import__("os").environ, "PYTHONPATH": str(src_dir)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "valid" in result.stdout


def test_subprocess_violations_exit_1(tmp_path: Path) -> None:
    """Verify the CLI exits 1 for violations when invoked as subprocess."""
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.0"
command = "cmd"

[[service]]
name = "svc"
target = "0.1"
command = "cmd"
"""
    _write_manifest(tmp_path, content)

    src_dir = Path(__file__).parent.parent / "src"
    result = subprocess.run(
        [sys.executable, "-m", "service_manifest.cli", "validate", str(tmp_path)],
        env={**__import__("os").environ, "PYTHONPATH": str(src_dir)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "svc" in result.stdout
