"""Tests for EnvWorkspaceLocator.

Coverage:
- WINTER_WORKSPACE_DIR env var is honored when set.
- Marker-walk fallback finds .winter/config.toml walking up from start_dir.
- worktree_dir(env) joins the env name onto workspace_root().
"""

from __future__ import annotations

import pytest

from service_orchestrator.core.internal.env_workspace_locator import EnvWorkspaceLocator


def test_workspace_root_honors_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    locator = EnvWorkspaceLocator()
    assert locator.workspace_root() == tmp_path


def test_workspace_root_env_var_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    locator = EnvWorkspaceLocator()
    root1 = locator.workspace_root()
    root2 = locator.workspace_root()
    assert root1 is root2


def test_workspace_root_marker_walk_finds_config(monkeypatch, tmp_path):
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    # Set up: workspace_root / .winter / config.toml
    marker_dir = tmp_path / ".winter"
    marker_dir.mkdir()
    (marker_dir / "config.toml").write_text("")

    # start_dir is several levels deep inside workspace_root
    start = tmp_path / "alpha" / "my-app" / "src"
    start.mkdir(parents=True)

    locator = EnvWorkspaceLocator(start_dir=start)
    assert locator.workspace_root() == tmp_path


def test_workspace_root_marker_walk_result_is_cached(monkeypatch, tmp_path):
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    marker_dir = tmp_path / ".winter"
    marker_dir.mkdir()
    (marker_dir / "config.toml").write_text("")

    start = tmp_path / "alpha"
    start.mkdir()

    locator = EnvWorkspaceLocator(start_dir=start)
    root1 = locator.workspace_root()
    root2 = locator.workspace_root()
    assert root1 is root2


def test_workspace_root_marker_walk_raises_when_not_found(monkeypatch, tmp_path):
    monkeypatch.delenv("WINTER_WORKSPACE_DIR", raising=False)

    # No .winter/config.toml anywhere in tmp_path
    start = tmp_path / "deep" / "nested"
    start.mkdir(parents=True)

    locator = EnvWorkspaceLocator(start_dir=start)
    with pytest.raises(RuntimeError, match="workspace root not found"):
        locator.workspace_root()


def test_worktree_dir_joins_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    locator = EnvWorkspaceLocator()
    assert locator.worktree_dir("alpha") == tmp_path / "alpha"


def test_worktree_dir_arbitrary_env_name(monkeypatch, tmp_path):
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    locator = EnvWorkspaceLocator()
    assert locator.worktree_dir("my-feature") == tmp_path / "my-feature"


# ---------------------------------------------------------------------------
# config_dir — WINTER_EXT_CONFIG_DIR env var + fallback
# ---------------------------------------------------------------------------


def test_config_dir_honors_env_var(monkeypatch, tmp_path):
    custom = tmp_path / "custom" / "config"
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("WINTER_EXT_CONFIG_DIR", str(custom))
    locator = EnvWorkspaceLocator()
    assert locator.config_dir() == custom


def test_config_dir_fallback_when_env_var_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    locator = EnvWorkspaceLocator()
    assert locator.config_dir() == tmp_path / ".winter" / "config" / "winter-service-tmux"


def test_config_dir_env_var_takes_priority_over_fallback(monkeypatch, tmp_path):
    """When WINTER_EXT_CONFIG_DIR is set, the fallback dir is NOT used."""
    override = tmp_path / "ext-override"
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("WINTER_EXT_CONFIG_DIR", str(override))
    locator = EnvWorkspaceLocator()
    result = locator.config_dir()
    assert result == override
    assert result != tmp_path / ".winter" / "config" / "winter-service-tmux"
