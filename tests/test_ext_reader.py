"""Tests for service_manifest.modules.manifest.ext_reader.ExtManifestMerger.

Covers:
- No env var → original manifest returned unchanged
- Extension service with valid target merged into services (feature-env scope)
- Extension service with workspace scope merged into workspace_services
- Service without target → skipped with warning
- Service with name collision against config.toml service → skipped with warning
- Malformed TOML file → original manifest returned (graceful degradation)
- Missing file → original manifest returned
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from service_manifest.modules.manifest.ext_reader import ExtManifestMerger
from service_manifest.modules.manifest.model import (
    LogConfig,
    Service,
    ServiceManifest,
    Target,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_MANIFEST = ServiceManifest(
    session_prefix="wws",
    env_file=".winter.env",
    layout_hook=None,
    services=(Service(name="api", target=Target(1, 0), cmd="uvicorn"),),
    workspace_services=(Service(name="rabbitmq", target=Target(0, 0), cmd="docker run rabbitmq"),),
    logs=LogConfig(),
    workspace_layout_hook=None,
)


def _merge(
    manifest_content: str, base: ServiceManifest = _BASE_MANIFEST, tmp_path: Path | None = None
) -> ServiceManifest:
    """Write manifest_content to a temp file and merge it into base."""
    # tmp_path provided by pytest fixture; for non-fixture callers we need a fallback.
    import tempfile

    if tmp_path is None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(textwrap.dedent(manifest_content))
            path = Path(f.name)
    else:
        path = tmp_path / "ext.toml"
        path.write_text(textwrap.dedent(manifest_content))
    return ExtManifestMerger().read_and_merge(base, path)


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


def test_empty_ext_manifest_returns_base(tmp_path: Path) -> None:
    result = _merge("", tmp_path=tmp_path)
    assert result is _BASE_MANIFEST


def test_no_service_table_returns_base(tmp_path: Path) -> None:
    result = _merge('[other]\nfoo = "bar"\n', tmp_path=tmp_path)
    assert result is _BASE_MANIFEST


def test_empty_service_list_returns_base(tmp_path: Path) -> None:
    """An empty [[service]] list returns the original manifest."""
    content = ""
    result = _merge(content, tmp_path=tmp_path)
    assert result is _BASE_MANIFEST


# ---------------------------------------------------------------------------
# Feature-env scope — services
# ---------------------------------------------------------------------------


def test_ext_service_with_target_merged_into_services(tmp_path: Path) -> None:
    """A feature-env service with a valid target is appended to manifest.services."""
    content = """\
[[service]]
name    = "worker"
scope   = "feature-environment"
source  = "my-ext"
cmd = "python -m worker"
target  = "2.0"
"""
    result = _merge(content, tmp_path=tmp_path)
    names = [s.name for s in result.services]
    assert "worker" in names
    assert "api" in names  # original preserved


def test_ext_service_target_parsed_correctly(tmp_path: Path) -> None:
    content = """\
[[service]]
name    = "worker"
scope   = "feature-environment"
source  = "my-ext"
cmd = "python -m worker"
target  = "3.1"
"""
    result = _merge(content, tmp_path=tmp_path)
    worker = next(s for s in result.services if s.name == "worker")
    assert worker.target.window == 3
    assert worker.target.pane == 1


def test_ext_service_default_scope_is_feature_env(tmp_path: Path) -> None:
    """scope defaults to feature-environment when omitted."""
    content = """\
[[service]]
name   = "worker"
source = "my-ext"
target = "2.0"
"""
    result = _merge(content, tmp_path=tmp_path)
    assert any(s.name == "worker" for s in result.services)
    assert not any(s.name == "worker" for s in result.workspace_services)


# ---------------------------------------------------------------------------
# Workspace scope — workspace_services
# ---------------------------------------------------------------------------


def test_ext_workspace_service_merged_into_workspace_services(tmp_path: Path) -> None:
    content = """\
[[service]]
name    = "postgres"
scope   = "workspace"
source  = "my-ext"
cmd = "pg_ctl start"
target  = "1.0"
"""
    result = _merge(content, tmp_path=tmp_path)
    ws_names = [s.name for s in result.workspace_services]
    assert "postgres" in ws_names
    assert "rabbitmq" in ws_names  # original preserved


# ---------------------------------------------------------------------------
# Skip / warning cases
# ---------------------------------------------------------------------------


def test_ext_service_without_target_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A service without a target is skipped; original manifest returned."""
    import logging

    content = """\
[[service]]
name    = "notarget"
scope   = "feature-environment"
source  = "my-ext"
cmd = "echo hi"
"""
    with caplog.at_level(logging.WARNING):
        result = _merge(content, tmp_path=tmp_path)

    assert not any(s.name == "notarget" for s in result.services)
    assert any("notarget" in r.message and "target" in r.message for r in caplog.records)


def test_ext_service_name_collision_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A service whose name collides with an existing config.toml service is skipped."""
    import logging

    content = """\
[[service]]
name    = "api"
scope   = "feature-environment"
source  = "my-ext"
cmd = "new-api"
target  = "3.0"
"""
    with caplog.at_level(logging.WARNING):
        result = _merge(content, tmp_path=tmp_path)

    # Only one "api" service — the original.
    api_services = [s for s in result.services if s.name == "api"]
    assert len(api_services) == 1
    assert api_services[0].cmd == "uvicorn"  # original cmd preserved
    assert any("api" in r.message for r in caplog.records)


def test_ext_service_missing_name_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    content = """\
[[service]]
scope   = "feature-environment"
source  = "my-ext"
target  = "2.0"
"""
    with caplog.at_level(logging.WARNING):
        result = _merge(content, tmp_path=tmp_path)

    assert result.services == _BASE_MANIFEST.services


def test_ext_service_malformed_target_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    content = """\
[[service]]
name    = "svc"
scope   = "feature-environment"
source  = "my-ext"
target  = "notanumber"
"""
    with caplog.at_level(logging.WARNING):
        result = _merge(content, tmp_path=tmp_path)

    assert not any(s.name == "svc" for s in result.services)


def test_ext_service_invalid_scope_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    content = """\
[[service]]
name    = "svc"
scope   = "bad-scope"
source  = "my-ext"
target  = "2.0"
"""
    with caplog.at_level(logging.WARNING):
        result = _merge(content, tmp_path=tmp_path)

    assert not any(s.name == "svc" for s in result.services)


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_malformed_toml_returns_base(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    path = tmp_path / "bad.toml"
    path.write_text("[[not valid toml", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = ExtManifestMerger().read_and_merge(_BASE_MANIFEST, path)

    assert result is _BASE_MANIFEST
    assert any("malformed" in r.message.lower() for r in caplog.records)


def test_missing_file_returns_base(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    missing = tmp_path / "nonexistent.toml"
    with caplog.at_level(logging.WARNING):
        result = ExtManifestMerger().read_and_merge(_BASE_MANIFEST, missing)

    assert result is _BASE_MANIFEST
    assert any("cannot read" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Multiple services
# ---------------------------------------------------------------------------


def test_multiple_ext_services_all_merged(tmp_path: Path) -> None:
    content = """\
[[service]]
name    = "svc-a"
scope   = "feature-environment"
source  = "my-ext"
target  = "2.0"

[[service]]
name    = "svc-b"
scope   = "workspace"
source  = "my-ext"
target  = "3.0"
"""
    result = _merge(content, tmp_path=tmp_path)
    assert any(s.name == "svc-a" for s in result.services)
    assert any(s.name == "svc-b" for s in result.workspace_services)
