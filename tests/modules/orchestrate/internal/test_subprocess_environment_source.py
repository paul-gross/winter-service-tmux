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
        'if [ "$1" != env ] || [ "$2" != alpha ]; then exit 2; fi\n'
        "printf \"export WINTER_ENV='alpha'\\nexport WTS_API_PORT='4020'\\n\"\n",
        encoding="utf-8",
    )
    winter.chmod(0o755)
    return {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


def test_scope_environment_uses_winter_env_even_when_provider_lacks_band(tmp_path: Path) -> None:
    source = SubprocessEnvironmentSource()
    base = _fake_winter(tmp_path)

    result = source.scope_environment("alpha", cwd=tmp_path, base=base, resolve_commands=False)

    assert "WTS_API_PORT" not in base
    assert result["WINTER_ENV"] == "alpha"
    assert result["WTS_API_PORT"] == "4020"


def test_scope_environment_captures_the_scope_path_unchanged(tmp_path: Path) -> None:
    source = SubprocessEnvironmentSource()
    base = _fake_winter(tmp_path)

    result = source.scope_environment("alpha", cwd=tmp_path, base=base, resolve_commands=False)

    assert result["PATH"] == base["PATH"]


def test_scope_environment_capture_survives_scope_path_override(tmp_path: Path) -> None:
    winter = tmp_path / "winter"
    winter.write_text(
        "#!/bin/sh\nprintf \"export PATH='/missing'\\nexport CAPTURED='scope'\\n\"\n",
        encoding="utf-8",
    )
    winter.chmod(0o755)
    source = SubprocessEnvironmentSource()

    result = source.scope_environment(
        "alpha",
        cwd=tmp_path,
        base={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        resolve_commands=False,
    )

    assert result["CAPTURED"] == "scope"
    assert result["PATH"] == "/missing"


def test_env_file_evaluation_survives_scope_path_override(tmp_path: Path) -> None:
    winter = tmp_path / "winter"
    winter.write_text(
        "#!/bin/sh\nprintf \"export PATH='/missing'\\nexport CAPTURED='scope'\\n\"\n",
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
        resolve_commands=False,
    )
    result = source.env_file_environment(env_file, cwd=tmp_path, base=scoped)

    assert result["PATH"] == "/missing"
    assert result["FROM_FILE"] == "available"


def _argv_echoing_winter(tmp_path: Path) -> dict[str, str]:
    """A fake ``winter`` that exports the argv it was handed, one word per var."""
    winter = tmp_path / "winter"
    winter.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        'for word in "$@"; do\n'
        "    i=$((i + 1))\n"
        '    printf "export ARGV_%s=\'%s\'\\n" "$i" "$word"\n'
        "done\n"
        'printf "export ARGC=\'%s\'\\n" "$#"\n',
        encoding="utf-8",
    )
    winter.chmod(0o755)
    return {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


def test_scope_environment_passes_resolve_when_commands_are_resolved(tmp_path: Path) -> None:
    """A launch-path read asks winter to run command-valued band entries."""
    source = SubprocessEnvironmentSource()

    result = source.scope_environment(
        "alpha",
        cwd=tmp_path,
        base=_argv_echoing_winter(tmp_path),
        resolve_commands=True,
    )

    assert result["ARGC"] == "3"
    assert [result["ARGV_1"], result["ARGV_2"], result["ARGV_3"]] == ["env", "alpha", "--resolve"]


def test_scope_environment_omits_the_flag_word_entirely_when_not_resolving(tmp_path: Path) -> None:
    """A reporting read passes two words, not a third empty one winter would reject."""
    source = SubprocessEnvironmentSource()

    result = source.scope_environment(
        "alpha",
        cwd=tmp_path,
        base=_argv_echoing_winter(tmp_path),
        resolve_commands=False,
    )

    assert result["ARGC"] == "2"
    assert [result["ARGV_1"], result["ARGV_2"]] == ["env", "alpha"]


def test_scope_environment_names_the_resolving_form_in_its_error(tmp_path: Path) -> None:
    """A failure under --resolve says so, so the gate is visible in the message."""
    winter = tmp_path / "winter"
    winter.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    winter.chmod(0o755)
    source = SubprocessEnvironmentSource()

    with pytest.raises(OrchestratorError, match=r"winter env alpha --resolve \(exit 7\)"):
        source.scope_environment(
            "alpha",
            cwd=tmp_path,
            base={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
            resolve_commands=True,
        )


def test_env_file_environment_matches_shell_scope_expansion(tmp_path: Path) -> None:
    source = SubprocessEnvironmentSource()
    env_file = tmp_path / ".winter.env"
    env_file.write_text(
        "GLOBAL_PORT=${WTS_API_PORT}\nSERVICE_URL=http://localhost:${GLOBAL_PORT}/health\n",
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
        SubprocessEnvironmentSource().env_file_environment(env_file, cwd=tmp_path, base={"PATH": os.environ["PATH"]})
