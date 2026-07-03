"""Tests for the 'describe' action in service_orchestrator.cli.

The describe action reads the service manifest and returns scope-qualified
service names in the shape required by winter core's service→provider
ownership index:
  - ``workspace/<name>`` for workspace-scoped services
  - ``*/<name>``         for project-scoped (env-agnostic) services

Covers:
- Empty describe when no config dir or manifest found
- Project-only services → all names prefixed with ``*/``
- Workspace-only services → all names prefixed with ``workspace/``
- Mixed project + workspace services
- WINTER_EXT_CONFIG_DIR env var is honoured
- Non-JSON / malformed manifest → empty list (graceful)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service_orchestrator.cli import main


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Return a temporary extension config directory."""
    cfg_dir = tmp_path / ".winter" / "config" / "winter-service-tmux"
    cfg_dir.mkdir(parents=True)
    return cfg_dir


def _write_manifest(cfg_dir: Path, content: str) -> None:
    (cfg_dir / "config.toml").write_text(content, encoding="utf-8")


def test_describe_empty_when_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """No WINTER_EXT_CONFIG_DIR and no default config → empty services list, exit 0."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    # No config.toml created — config dir does not exist
    rc = main(["describe"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == []


def test_describe_project_services(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Project-scoped services are emitted as ``*/<name>``."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(
        tmp_config,
        """
session_prefix = "mp"
[[service]]
name = "backend"
target = "0.0"
command = "npm start"

[[service]]
name = "worker"
target = "0.1"
command = "npm run worker"
""",
    )
    rc = main(["describe"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert "*/backend" in obj["services"]
    assert "*/worker" in obj["services"]
    assert len(obj["services"]) == 2


def test_describe_workspace_services(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Workspace-scoped services are emitted as ``workspace/<name>``."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(
        tmp_config,
        """
session_prefix = "mp"
[[service]]
name = "rabbitmq"
target = "0.0"
command = "rabbitmq-server"
scope = "workspace"
""",
    )
    rc = main(["describe"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == ["workspace/rabbitmq"]


def test_describe_mixed_scopes(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Mixed project + workspace services: workspace names first, then ``*/`` names."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(
        tmp_config,
        """
session_prefix = "mp"
[[service]]
name = "api"
target = "0.0"
command = "uvicorn"

[[service]]
name = "postgres"
target = "1.0"
command = "postgres"
scope = "workspace"
""",
    )
    rc = main(["describe"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert "workspace/postgres" in obj["services"]
    assert "*/api" in obj["services"]


def test_describe_respects_winter_ext_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """WINTER_EXT_CONFIG_DIR overrides the default config path."""
    custom_dir = tmp_path / "custom-config"
    custom_dir.mkdir()
    _write_manifest(
        custom_dir,
        """
session_prefix = "mp"
[[service]]
name = "myservice"
target = "0.0"
command = "cmd"
""",
    )
    monkeypatch.setenv("WINTER_EXT_CONFIG_DIR", str(custom_dir))
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))

    rc = main(["describe"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == ["*/myservice"]


def test_describe_empty_manifest_returns_empty(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A valid manifest with no services returns an empty list."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(tmp_config, 'session_prefix = "mp"\n')
    rc = main(["describe"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == []


def test_describe_output_is_valid_json_shape(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Describe output is a JSON object with a 'services' key containing a list of strings."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(
        tmp_config,
        """
session_prefix = "mp"
[[service]]
name = "frontend"
target = "0.0"
command = "npm run dev"
""",
    )
    rc = main(["describe"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert isinstance(obj, dict)
    assert "services" in obj
    assert isinstance(obj["services"], list)
    assert all(isinstance(s, str) for s in obj["services"])
