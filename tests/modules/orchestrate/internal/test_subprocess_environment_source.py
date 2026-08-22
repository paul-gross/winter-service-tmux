"""Regression tests for the canonical shell environment source."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.internal.subprocess_environment_source import (
    SubprocessEnvironmentSource,
)


def _fake_winter(tmp_path: Path) -> dict[str, str]:
    winter = tmp_path / "winter"
    winter.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" != env ] || [ \"$2\" != alpha ]; then exit 2; fi\n"
        "printf \"export WINTER_ENV='alpha'\\nexport WTS_API_PORT='4020'\\n\"\n",
        encoding="utf-8",
    )
    winter.chmod(0o755)
    return {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


def test_scope_environment_uses_winter_env_even_when_provider_lacks_band(tmp_path: Path) -> None:
    source = SubprocessEnvironmentSource()
    base = _fake_winter(tmp_path)

    result = source.scope_environment("alpha", cwd=tmp_path, base=base)

    assert "WTS_API_PORT" not in base
    assert result["WINTER_ENV"] == "alpha"
    assert result["WTS_API_PORT"] == "4020"


def test_scope_environment_capture_survives_scope_path_override(tmp_path: Path) -> None:
    winter = tmp_path / "winter"
    winter.write_text(
        "#!/bin/sh\n"
        "printf \"export PATH='/missing'\\nexport CAPTURED='scope'\\n\"\n",
        encoding="utf-8",
    )
    winter.chmod(0o755)
    source = SubprocessEnvironmentSource()

    result = source.scope_environment(
        "alpha",
        cwd=tmp_path,
        base={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result["CAPTURED"] == "scope"
    assert result["PATH"] == "/missing"


def test_env_file_evaluation_survives_scope_path_override(tmp_path: Path) -> None:
    winter = tmp_path / "winter"
    winter.write_text(
        "#!/bin/sh\n"
        "printf \"export PATH='/missing'\\nexport CAPTURED='scope'\\n\"\n",
        encoding="utf-8",
    )
    winter.chmod(0o755)
    env_file = tmp_path / ".winter.env"
    env_file.write_text("FROM_FILE=available\n", encoding="utf-8")
    source = SubprocessEnvironmentSource()

    scoped = source.scope_environment(
        "alpha",
        cwd=tmp_path,
        base={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
    )
    result = source.env_file_environment(env_file, cwd=tmp_path, base=scoped)

    assert result["PATH"] == "/missing"
    assert result["FROM_FILE"] == "available"


def test_env_file_environment_matches_shell_scope_expansion(tmp_path: Path) -> None:
    source = SubprocessEnvironmentSource()
    env_file = tmp_path / ".winter.env"
    env_file.write_text(
        "GLOBAL_PORT=${WTS_API_PORT}\n"
        "SERVICE_URL=http://localhost:${GLOBAL_PORT}/health\n",
        encoding="utf-8",
    )

    result = source.env_file_environment(
        env_file,
        cwd=tmp_path,
        base={"PATH": os.environ["PATH"], "WTS_API_PORT": "4020"},
    )

    assert result["GLOBAL_PORT"] == "4020"
    assert result["SERVICE_URL"] == "http://localhost:4020/health"


def test_env_file_environment_capture_survives_env_file_path_override(tmp_path: Path) -> None:
    source = SubprocessEnvironmentSource()
    env_file = tmp_path / ".winter.env"
    env_file.write_text("PATH=/missing\nCAPTURED=env-file\n", encoding="utf-8")

    result = source.env_file_environment(
        env_file,
        cwd=tmp_path,
        base={"PATH": os.environ["PATH"]},
    )

    assert result["CAPTURED"] == "env-file"
    assert result["PATH"] == "/missing"


def test_missing_env_file_is_a_preflight_error(tmp_path: Path) -> None:
    source = SubprocessEnvironmentSource()
    base = {"PATH": os.environ["PATH"], "WINTER_ENV": "alpha"}

    with pytest.raises(OrchestratorError, match="file does not exist"):
        source.env_file_environment(tmp_path / ".missing.env", cwd=tmp_path, base=base)


def test_env_file_ignores_intermediate_failure_when_final_command_succeeds(tmp_path: Path) -> None:
    env_file = tmp_path / ".winter.env"
    env_file.write_text("false\nSURVIVED=1\n", encoding="utf-8")

    result = SubprocessEnvironmentSource().env_file_environment(
        env_file, cwd=tmp_path, base={"PATH": os.environ["PATH"]}
    )

    assert result["SURVIVED"] == "1"


def test_env_file_fails_when_source_final_command_fails(tmp_path: Path) -> None:
    env_file = tmp_path / ".winter.env"
    env_file.write_text("SURVIVED=1\nfalse\n", encoding="utf-8")

    with pytest.raises(OrchestratorError, match=r"source .*exit 1"):
        SubprocessEnvironmentSource().env_file_environment(
            env_file, cwd=tmp_path, base={"PATH": os.environ["PATH"]}
        )
