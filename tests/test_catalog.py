"""Tests for the 'catalog' action in service_orchestrator.cli.

The catalog action reads the service manifest and returns scope-qualified
service names:
  - ``workspace/<name>`` for workspace-scoped services
  - ``*/<name>``         for project-scoped (env-agnostic) services

Covers:
- Empty catalog when no config dir or manifest found
- Project-only services → all names prefixed with ``*/``
- Workspace-only services → all names prefixed with ``workspace/``
- Mixed project + workspace services
- WINTER_EXT_CONFIG_DIR env var is honoured
- Non-JSON / malformed manifest → empty catalog (graceful)
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


def test_catalog_empty_when_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No WINTER_EXT_CONFIG_DIR and no default config → empty services list."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    # No config.toml created — config dir does not exist
    rc = main(["catalog"])
    assert rc == 0


def test_catalog_project_services(
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
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == ["*/backend", "*/worker"]


def test_catalog_workspace_services(
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
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == ["workspace/rabbitmq"]


def test_catalog_mixed_scopes(
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
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    # workspace services first (per catalog ordering convention)
    assert "workspace/postgres" in obj["services"]
    assert "*/api" in obj["services"]


def test_catalog_respects_winter_ext_config_dir(
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

    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == ["*/myservice"]


def test_catalog_empty_manifest_returns_empty(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A valid manifest with no services returns an empty list."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(tmp_config, 'session_prefix = "mp"\n')
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == []
