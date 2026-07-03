"""Tests for service_manifest.modules.manifest.reader — TOML parse, overlay merge, ManifestError paths."""

from pathlib import Path

import pytest

from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import (
    Health,
    HealthType,
    LogConfig,
    LogMode,
    Service,
    ServiceManifest,
    StartupPolicy,
    Target,
)
from service_manifest.modules.manifest.reader import ManifestReader
from tests.fakes import FakeFilesystemReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The reader now accepts a config_dir directly (no _MANIFEST_DIR joining).
# Tests pass absolute paths keyed to the fake config dir.
_CONFIG_DIR = Path("/fake/workspace/.winter/config/winter-service-tmux")
_COMMITTED_PATH = Path("config.toml")
_LOCAL_PATH = Path("config.local.toml")

_ROOT = Path("/fake/workspace")


def _reader_with(files: dict[Path, str]) -> ManifestReader:
    """Build a ManifestReader over a FakeFilesystemReader seeded with *files*.

    Keys in *files* are joined to the fake config dir so callers can
    write relative paths like ``_COMMITTED_PATH``.
    """
    abs_files = {_CONFIG_DIR / k: v for k, v in files.items()}
    return ManifestReader(FakeFilesystemReader(abs_files))


def _read(files: dict[Path, str]) -> ServiceManifest:
    return _reader_with(files).read(_CONFIG_DIR)


# ---------------------------------------------------------------------------
# Valid parse — scalars, services, target parsing
# ---------------------------------------------------------------------------


def test_valid_full_manifest() -> None:
    content = """\
session_prefix = "mp"
env_file = ".winter.env"
layout_hook = "layout-hook.sh"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[[service]]
name = "frontend"
target = "0.1"
cmd = "npm run dev"

[[service]]
name = "shell"
target = "1.0"
cmd = ""
"""
    manifest = _read({_COMMITTED_PATH: content})

    assert isinstance(manifest, ServiceManifest)
    assert manifest.session_prefix == "mp"
    assert manifest.env_file == ".winter.env"
    assert manifest.layout_hook == "layout-hook.sh"

    assert len(manifest.services) == 3
    assert manifest.services[0] == Service(name="backend", target=Target(window=0, pane=0), cmd="npm run start:dev")
    assert manifest.services[1] == Service(name="frontend", target=Target(window=0, pane=1), cmd="npm run dev")
    assert manifest.services[2] == Service(name="shell", target=Target(window=1, pane=0), cmd="")


def test_valid_minimal_manifest() -> None:
    """session_prefix is optional; all top-level fields may be absent."""
    manifest = _read({_COMMITTED_PATH: 'session_prefix = "mp"\n'})

    assert manifest.session_prefix == "mp"
    assert manifest.env_file is None
    assert manifest.layout_hook is None
    assert manifest.services == ()


def test_valid_manifest_with_no_top_level_scalars() -> None:
    """Nothing is required at the top level: session_prefix absent → None."""
    manifest = _read({_COMMITTED_PATH: ""})

    assert manifest.session_prefix is None
    assert manifest.env_file is None
    assert manifest.layout_hook is None
    assert manifest.services == ()


def test_target_parsed_to_correct_window_and_pane() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "2.3"
cmd = "echo hi"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].target == Target(window=2, pane=3)


def test_service_health_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.health]
type = "url"
target = "http://localhost:${BACKEND_PORT}/health"
timeout = 2
"""
    manifest = _read({_COMMITTED_PATH: content})

    assert manifest.services[0].health == Health(
        type=HealthType.URL,
        target="http://localhost:${BACKEND_PORT}/health",
        timeout=2.0,
    )


def test_service_health_cmd_parsed_without_timeout() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "worker"
target = "0.0"
cmd = "npm run worker"

[service.health]
type = "cmd"
target = "pgrep -f worker"
"""
    manifest = _read({_COMMITTED_PATH: content})

    assert manifest.services[0].health == Health(type=HealthType.CMD, target="pgrep -f worker")


def test_service_startup_parsed_with_both_keys() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.startup]
retries = 3
retry_delay = 2
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].startup == StartupPolicy(retries=3, retry_delay=2.0)


def test_service_startup_retry_delay_omitted_uses_default() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.startup]
retries = 5
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].startup == StartupPolicy(retries=5, retry_delay=2.0)


def test_service_startup_retries_omitted_uses_default() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.startup]
retry_delay = 0.5
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].startup == StartupPolicy(retries=0, retry_delay=0.5)


def test_service_startup_absent_is_none() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].startup is None


def test_overlay_service_startup_overrides_by_name() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.startup]
retries = 2
retry_delay = 1.0
"""
    overlay = """\
[[service]]
name = "backend"

[service.startup]
retries = 5
retry_delay = 3.0
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert manifest.services[0].startup == StartupPolicy(retries=5, retry_delay=3.0)


def test_service_startup_malformed_retries_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"

[service.startup]
retries = "x"
"""
    with pytest.raises(ManifestError, match=r"startup\.retries"):
        _read({_COMMITTED_PATH: content})


def test_missing_optional_fields_are_none() -> None:
    content = """\
session_prefix = "proj"

[[service]]
name = "worker"
target = "0.0"
cmd = "python -m worker"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.env_file is None
    assert manifest.layout_hook is None


# ---------------------------------------------------------------------------
# Error paths — missing file, malformed TOML, missing required fields
# ---------------------------------------------------------------------------


def test_missing_committed_file_raises() -> None:
    with pytest.raises(ManifestError, match="not found"):
        _read({})


def test_malformed_toml_raises() -> None:
    with pytest.raises(ManifestError, match="malformed TOML"):
        _read({_COMMITTED_PATH: "this is not [ valid toml !!!\n"})


def test_missing_session_prefix_yields_none() -> None:
    """session_prefix is optional; absent means resolution falls back to
    WINTER_SERVICE_PREFIX (SessionContextBuilder's concern, not the reader's)."""
    manifest = _read({_COMMITTED_PATH: 'env_file = ".env"\n'})
    assert manifest.session_prefix is None


def test_empty_session_prefix_raises() -> None:
    """A declared session_prefix must be non-empty when present."""
    with pytest.raises(ManifestError, match="session_prefix"):
        _read({_COMMITTED_PATH: 'session_prefix = ""\n'})


def test_service_missing_name_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
target = "0.0"
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="name"):
        _read({_COMMITTED_PATH: content})


def test_service_missing_target_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
cmd = "npm start"
"""
    with pytest.raises(ManifestError, match="target"):
        _read({_COMMITTED_PATH: content})


def test_service_health_unknown_type_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"

[service.health]
type = "tcp"
target = "localhost:3000"
"""
    with pytest.raises(ManifestError, match=r"health\.type"):
        _read({_COMMITTED_PATH: content})


def test_service_health_missing_target_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"

[service.health]
type = "url"
"""
    with pytest.raises(ManifestError, match=r"health\.target"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# Malformed target strings
# ---------------------------------------------------------------------------


def test_malformed_target_non_integer_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "abc"
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


def test_malformed_target_single_integer_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0"
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


def test_malformed_target_extra_dots_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.1.2"
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


def test_malformed_target_letters_in_parts_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "a.b"
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# Overlay semantics — no overlay file present
# ---------------------------------------------------------------------------


def test_no_overlay_file_reads_committed() -> None:
    manifest = _read({_COMMITTED_PATH: 'session_prefix = "mp"\n'})
    assert manifest.session_prefix == "mp"


# ---------------------------------------------------------------------------
# Overlay semantics — scalar overrides
# ---------------------------------------------------------------------------


def test_overlay_overrides_session_prefix() -> None:
    manifest = _read(
        {
            _COMMITTED_PATH: 'session_prefix = "committed"\n',
            _LOCAL_PATH: 'session_prefix = "local"\n',
        }
    )
    assert manifest.session_prefix == "local"


def test_overlay_sets_session_prefix_when_committed_omits_it() -> None:
    """A machine-local override can opt in to a manifest override even when
    the committed config.toml declares none (relying on WINTER_SERVICE_PREFIX)."""
    manifest = _read(
        {
            _COMMITTED_PATH: "",
            _LOCAL_PATH: 'session_prefix = "local"\n',
        }
    )
    assert manifest.session_prefix == "local"


def test_overlay_overrides_env_file() -> None:
    committed = """\
session_prefix = "mp"
env_file = ".winter.env"
"""
    manifest = _read(
        {
            _COMMITTED_PATH: committed,
            _LOCAL_PATH: 'env_file = ".local.env"\n',
        }
    )
    assert manifest.env_file == ".local.env"


def test_overlay_overrides_layout_hook() -> None:
    committed = """\
session_prefix = "mp"
layout_hook = "layout-hook.sh"
"""
    manifest = _read(
        {
            _COMMITTED_PATH: committed,
            _LOCAL_PATH: 'layout_hook = "layout-hook.local.sh"\n',
        }
    )
    assert manifest.layout_hook == "layout-hook.local.sh"


def test_overlay_absent_scalar_uses_committed() -> None:
    committed = """\
session_prefix = "mp"
env_file = ".winter.env"
"""
    manifest = _read(
        {
            _COMMITTED_PATH: committed,
            _LOCAL_PATH: 'session_prefix = "local"\n',
        }
    )
    assert manifest.env_file == ".winter.env"


# ---------------------------------------------------------------------------
# Overlay semantics — service override-by-name
# ---------------------------------------------------------------------------


def test_overlay_service_overrides_by_name() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
"""
    overlay = """\
[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:prod"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.services) == 1
    assert manifest.services[0].cmd == "npm run start:prod"


def test_overlay_service_overrides_target_field() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "cmd"
"""
    overlay = """\
[[service]]
name = "backend"
target = "1.0"
cmd = "cmd"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert manifest.services[0].target == Target(window=1, pane=0)


def test_overlay_service_overrides_health_by_name() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "cmd"

[service.health]
type = "url"
target = "http://localhost:${BACKEND_PORT}/health"
"""
    overlay = """\
[[service]]
name = "backend"

[service.health]
type = "cmd"
target = "pgrep -f backend"
timeout = 1
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert manifest.services[0].health == Health(type=HealthType.CMD, target="pgrep -f backend", timeout=1.0)


# ---------------------------------------------------------------------------
# Overlay semantics — service append (new name)
# ---------------------------------------------------------------------------


def test_overlay_service_append_new_name() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
"""
    overlay = """\
[[service]]
name = "worker"
target = "1.0"
cmd = "python -m worker"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.services) == 2
    assert manifest.services[0].name == "backend"
    assert manifest.services[1].name == "worker"


def test_overlay_preserves_committed_order_with_override_and_append() -> None:
    """Committed order is preserved; overrides stay in place, appends follow."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "backend-cmd"

[[service]]
name = "frontend"
target = "0.1"
cmd = "frontend-cmd"
"""
    overlay = """\
[[service]]
name = "frontend"
target = "0.1"
cmd = "frontend-override"

[[service]]
name = "shell"
target = "1.0"
cmd = ""
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.services) == 3
    # backend unchanged at position 0
    assert manifest.services[0].name == "backend"
    assert manifest.services[0].cmd == "backend-cmd"
    # frontend overridden in place at position 1
    assert manifest.services[1].name == "frontend"
    assert manifest.services[1].cmd == "frontend-override"
    # shell appended at position 2
    assert manifest.services[2].name == "shell"


# ---------------------------------------------------------------------------
# [[status.url]] entries are silently ignored
# ---------------------------------------------------------------------------


def test_status_url_entries_silently_ignored() -> None:
    """[[status.url]] is a removed feature — present entries are silently ignored."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = "npm start"

[[status.url]]
label = "Backend"
url = "http://localhost:${BACKEND_PORT}"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.session_prefix == "mp"
    assert len(manifest.services) == 1


# ---------------------------------------------------------------------------
# Malformed overlay TOML raises ManifestError
# ---------------------------------------------------------------------------


def test_malformed_overlay_toml_raises() -> None:
    with pytest.raises(ManifestError, match="malformed TOML"):
        _read(
            {
                _COMMITTED_PATH: 'session_prefix = "mp"\n',
                _LOCAL_PATH: "this is [ not valid toml !!!\n",
            }
        )


# ---------------------------------------------------------------------------
# Non-string target field raises ManifestError (type guard — Fix 1)
# ---------------------------------------------------------------------------


def test_float_target_zero_raises() -> None:
    """target = 0.0 (TOML float) must be rejected — not silently coerced."""
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = 0.0
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="quoted string"):
        _read({_COMMITTED_PATH: content})


def test_float_target_1_10_raises() -> None:
    """target = 1.10 (TOML float) parses as 1.1 and must be rejected."""
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = 1.10
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="quoted string"):
        _read({_COMMITTED_PATH: content})


def test_integer_target_raises() -> None:
    """target = 1 (TOML integer) must be rejected."""
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = 1
cmd = "cmd"
"""
    with pytest.raises(ManifestError, match="quoted string"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# [logs] table parsing
# ---------------------------------------------------------------------------


def test_full_logs_table_parsed() -> None:
    content = """\
session_prefix = "mp"

[logs]
rotate_size_bytes = 2048
max_rotations = 3
retention_seconds = 86400
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.logs == LogConfig(rotate_size_bytes=2048, max_rotations=3, retention_seconds=86400)


def test_partial_logs_table_fills_defaults() -> None:
    """Only max_rotations set — the other two fields use LogConfig defaults."""
    content = """\
session_prefix = "mp"

[logs]
max_rotations = 10
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.logs.max_rotations == 10
    assert manifest.logs.rotate_size_bytes == LogConfig().rotate_size_bytes
    assert manifest.logs.retention_seconds == LogConfig().retention_seconds


def test_absent_logs_table_uses_defaults() -> None:
    manifest = _read({_COMMITTED_PATH: 'session_prefix = "mp"\n'})
    assert manifest.logs == LogConfig()


def test_logs_non_int_field_raises() -> None:
    content = """\
session_prefix = "mp"

[logs]
rotate_size_bytes = "big"
"""
    with pytest.raises(ManifestError, match="rotate_size_bytes"):
        _read({_COMMITTED_PATH: content})


def test_logs_non_int_max_rotations_raises() -> None:
    content = """\
session_prefix = "mp"

[logs]
max_rotations = 5.5
"""
    with pytest.raises(ManifestError, match="max_rotations"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# [logs] overlay — per-key merge
# ---------------------------------------------------------------------------


def test_overlay_logs_single_key_overrides_only_that_key() -> None:
    """Local overlay with one [logs] key keeps committed values for the rest."""
    committed = """\
session_prefix = "mp"

[logs]
rotate_size_bytes = 2048
max_rotations = 3
retention_seconds = 86400
"""
    local = """\
[logs]
rotate_size_bytes = 52428800
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: local})
    assert manifest.logs.rotate_size_bytes == 52428800
    assert manifest.logs.max_rotations == 3
    assert manifest.logs.retention_seconds == 86400


def test_overlay_logs_with_no_committed_logs_uses_defaults_plus_override() -> None:
    """No committed [logs] table; local sets one key — defaults fill the rest."""
    committed = 'session_prefix = "mp"\n'
    local = """\
[logs]
retention_seconds = 0
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: local})
    assert manifest.logs.retention_seconds == 0
    assert manifest.logs.rotate_size_bytes == LogConfig().rotate_size_bytes
    assert manifest.logs.max_rotations == LogConfig().max_rotations


# ---------------------------------------------------------------------------
# Per-service log field
# ---------------------------------------------------------------------------


def test_service_log_pane_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "watcher"
target = "1.0"
cmd = "npm run watch"
log = "pane"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].log == LogMode.PANE


def test_service_log_file_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
log = "file"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].log == LogMode.FILE


def test_service_log_memory_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.0"
cmd = "cmd"
log = "memory"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].log == LogMode.MEMORY


def test_service_log_defaults_to_file_when_absent() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].log == LogMode.FILE


def test_service_log_invalid_string_raises_naming_allowed_values() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.0"
cmd = "cmd"
log = "yes"
"""
    with pytest.raises(ManifestError, match="'log'"):
        _read({_COMMITTED_PATH: content})


def test_service_log_non_string_raises() -> None:
    """A non-string log value (e.g. boolean true) must be rejected with a clear error."""
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.0"
cmd = "cmd"
log = true
"""
    with pytest.raises(ManifestError, match="'log'"):
        _read({_COMMITTED_PATH: content})


def test_overlay_service_log_pane_inherits_through_merge() -> None:
    """The per-service log field rides the existing {**committed, **overlay} merge."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"
log = "pane"
"""
    # Local overlay changes command only — log="pane" should be preserved.
    local = """\
[[service]]
name = "backend"
cmd = "npm run start:debug"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: local})
    assert manifest.services[0].cmd == "npm run start:debug"
    assert manifest.services[0].log == LogMode.PANE


def test_overlay_service_can_override_log_to_file() -> None:
    """Local overlay can change log mode for a committed service."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "watcher"
target = "0.0"
cmd = "npm run watch"
log = "pane"
"""
    local = """\
[[service]]
name = "watcher"
log = "file"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: local})
    assert manifest.services[0].log == LogMode.FILE


# ---------------------------------------------------------------------------
# workspace_services and workspace_layout_hook — parse + validate
# ---------------------------------------------------------------------------


def test_workspace_services_parsed() -> None:
    """[[service]] entries with scope="workspace" partition into workspace_services."""
    content = """\
session_prefix = "mp"
workspace_layout_hook = "context/project/workspace-layout-hook.sh"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"

[[service]]
name = "watcher"
target = "0.1"
cmd = "npm run watch"
log = "pane"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: content})

    assert len(manifest.workspace_services) == 2
    assert manifest.workspace_services[0].name == "docker"
    assert manifest.workspace_services[0].target.window == 0
    assert manifest.workspace_services[0].target.pane == 0
    assert manifest.workspace_services[0].cmd == "docker compose up"
    assert manifest.workspace_services[1].name == "watcher"
    assert manifest.workspace_services[1].log == LogMode.PANE
    assert manifest.workspace_layout_hook == "context/project/workspace-layout-hook.sh"


def test_services_partitioned_by_scope() -> None:
    """A single [[service]] array splits into project / workspace lists by scope."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert [s.name for s in manifest.services] == ["backend"]
    assert [s.name for s in manifest.workspace_services] == ["docker"]


def test_service_scope_defaults_to_project() -> None:
    """A [[service]] entry without scope is a project service."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert [s.name for s in manifest.services] == ["backend"]
    assert manifest.workspace_services == ()


def test_explicit_project_scope_is_project_service() -> None:
    """scope = "project" written explicitly routes to the project list."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"
scope = "project"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert [s.name for s in manifest.services] == ["backend"]
    assert manifest.workspace_services == ()


def test_unknown_scope_raises() -> None:
    """An unrecognised scope value is rejected with a clear message."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"
scope = "global"
"""
    with pytest.raises(ManifestError, match="invalid scope"):
        _read({_COMMITTED_PATH: content})


def test_scope_is_not_carried_onto_runtime_service() -> None:
    """scope is config-only — it is consumed at parse time, not stored on Service."""
    content = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert not hasattr(manifest.workspace_services[0], "scope")


def test_workspace_layout_hook_parsed() -> None:
    """workspace_layout_hook scalar is parsed independently of layout_hook."""
    content = """\
session_prefix = "mp"
layout_hook = "layout-hook.sh"
workspace_layout_hook = "workspace-layout-hook.sh"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.layout_hook == "layout-hook.sh"
    assert manifest.workspace_layout_hook == "workspace-layout-hook.sh"


def test_workspace_services_absent_defaults_empty() -> None:
    """No [[workspace_service]] section → workspace_services is an empty tuple."""
    manifest = _read({_COMMITTED_PATH: 'session_prefix = "mp"\n'})
    assert manifest.workspace_services == ()
    assert manifest.workspace_layout_hook is None


def test_workspace_service_log_defaults_to_file() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.workspace_services[0].log == LogMode.FILE


def test_workspace_service_missing_name_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
target = "0.0"
cmd = "cmd"
scope = "workspace"
"""
    with pytest.raises(ManifestError, match="name"):
        _read({_COMMITTED_PATH: content})


def test_workspace_service_missing_target_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "docker"
cmd = "cmd"
scope = "workspace"
"""
    with pytest.raises(ManifestError, match="target"):
        _read({_COMMITTED_PATH: content})


def test_workspace_service_malformed_target_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "abc"
cmd = "cmd"
scope = "workspace"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


def test_workspace_service_float_target_raises() -> None:
    """target = 0.0 (TOML float) must be rejected for a workspace-scoped service too."""
    content = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = 0.0
cmd = "cmd"
scope = "workspace"
"""
    with pytest.raises(ManifestError, match="quoted string"):
        _read({_COMMITTED_PATH: content})


def test_workspace_service_invalid_log_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "cmd"
log = "invalid"
scope = "workspace"
"""
    with pytest.raises(ManifestError, match="'log'"):
        _read({_COMMITTED_PATH: content})


def test_workspace_service_non_string_log_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "cmd"
log = true
scope = "workspace"
"""
    with pytest.raises(ManifestError, match="'log'"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# workspace-scoped service overlay semantics (scope = "workspace")
# ---------------------------------------------------------------------------


def test_overlay_workspace_service_overrides_by_name() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"
"""
    overlay = """\
[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up --build"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.workspace_services) == 1
    assert manifest.workspace_services[0].cmd == "docker compose up --build"


def test_overlay_workspace_service_append_new_name() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"
"""
    overlay = """\
[[service]]
name = "monitor"
target = "0.1"
cmd = "python -m monitor"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.workspace_services) == 2
    assert manifest.workspace_services[0].name == "docker"
    assert manifest.workspace_services[1].name == "monitor"


def test_overlay_workspace_service_preserves_order() -> None:
    """Override stays in place; append follows all committed entries."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"

[[service]]
name = "registry"
target = "0.1"
cmd = "docker run registry"
scope = "workspace"
"""
    overlay = """\
[[service]]
name = "registry"
target = "0.1"
cmd = "docker run registry:2"
scope = "workspace"

[[service]]
name = "monitor"
target = "1.0"
cmd = "python -m monitor"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.workspace_services) == 3
    assert manifest.workspace_services[0].name == "docker"
    assert manifest.workspace_services[1].name == "registry"
    assert manifest.workspace_services[1].cmd == "docker run registry:2"
    assert manifest.workspace_services[2].name == "monitor"


def test_overlay_service_scope_travels_through_merge() -> None:
    """An overlay override keeps its scope, and a workspace-scoped append routes correctly."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"
"""
    # Override the workspace docker entry (changing only command); append a new
    # workspace service. Both must land in the workspace list, not the project one.
    overlay = """\
[[service]]
name = "docker"
cmd = "docker compose up --build"
scope = "workspace"

[[service]]
name = "monitor"
target = "1.0"
cmd = "python -m monitor"
scope = "workspace"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert [s.name for s in manifest.services] == ["backend"]
    assert [s.name for s in manifest.workspace_services] == ["docker", "monitor"]
    assert manifest.workspace_services[0].cmd == "docker compose up --build"


def test_overlay_workspace_layout_hook_replaces_committed() -> None:
    committed = """\
session_prefix = "mp"
workspace_layout_hook = "context/project/workspace-layout-hook.sh"
"""
    overlay = 'workspace_layout_hook = "context/project/workspace-layout-hook.local.sh"\n'
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert manifest.workspace_layout_hook == "context/project/workspace-layout-hook.local.sh"


def test_overlay_workspace_service_partial_override_keeps_existing_fields() -> None:
    """Partial overlay (only command changed) keeps the committed log + scope."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
log = "pane"
scope = "workspace"
"""
    # Overlay omits scope; the committed scope = "workspace" rides the merge.
    overlay = """\
[[service]]
name = "docker"
cmd = "docker compose up --build"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert manifest.workspace_services[0].cmd == "docker compose up --build"
    assert manifest.workspace_services[0].log == LogMode.PANE
    assert [s.name for s in manifest.services] == []


def test_env_services_and_workspace_services_can_share_same_target() -> None:
    """An env service and a workspace service may use the same target (different sessions)."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm start"

[[service]]
name = "docker"
target = "0.0"
cmd = "docker compose up"
scope = "workspace"
"""
    # Should parse without error
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].target.window == 0
    assert manifest.services[0].target.pane == 0
    assert manifest.workspace_services[0].target.window == 0
    assert manifest.workspace_services[0].target.pane == 0


# ---------------------------------------------------------------------------
# read(config_dir) — config dir is passed directly; no _MANIFEST_DIR joining
# ---------------------------------------------------------------------------


def test_read_takes_config_dir_directly() -> None:
    """read() receives the config dir directly — config.toml lives at the root of that dir."""
    content = 'session_prefix = "mp"\n'
    # The committed file is at _CONFIG_DIR / "config.toml" — no context/project/ prefix.
    abs_committed = _CONFIG_DIR / "config.toml"
    fake_fs = FakeFilesystemReader({abs_committed: content})
    reader = ManifestReader(fake_fs)
    manifest = reader.read(_CONFIG_DIR)
    assert manifest.session_prefix == "mp"


def test_read_config_dir_missing_committed_raises() -> None:
    """When config.toml is absent in the config dir, ManifestError is raised."""
    fake_fs = FakeFilesystemReader({})  # nothing
    reader = ManifestReader(fake_fs)
    with pytest.raises(ManifestError, match="not found"):
        reader.read(_CONFIG_DIR)


def test_read_config_dir_reads_local_overlay() -> None:
    """config.local.toml in the config dir is merged on top of config.toml."""
    committed = 'session_prefix = "committed"\n'
    local = 'session_prefix = "local"\n'
    abs_committed = _CONFIG_DIR / "config.toml"
    abs_local = _CONFIG_DIR / "config.local.toml"
    fake_fs = FakeFilesystemReader({abs_committed: committed, abs_local: local})
    reader = ManifestReader(fake_fs)
    manifest = reader.read(_CONFIG_DIR)
    assert manifest.session_prefix == "local"


# ---------------------------------------------------------------------------
# cmd / command key — canonical key, deprecated alias, and absent key
# ---------------------------------------------------------------------------


def test_cmd_key_parsed() -> None:
    """The canonical 'cmd' key is read correctly into Service.cmd."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].cmd == "npm run start:dev"


def test_legacy_command_key_emits_deprecation_warning_and_is_read(capsys: pytest.CaptureFixture[str]) -> None:
    """The legacy 'command' key is accepted but emits a deprecation warning to stderr."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = "npm run start:dev"
"""
    manifest = _read({_COMMITTED_PATH: content})
    captured = capsys.readouterr()
    assert manifest.services[0].cmd == "npm run start:dev"
    assert "deprecation" in captured.err
    assert "backend" in captured.err
    assert "'cmd'" in captured.err


def test_cmd_takes_precedence_over_command_when_both_present(capsys: pytest.CaptureFixture[str]) -> None:
    """When both 'cmd' and 'command' are present, 'cmd' is canonical and wins; no warning."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "npm run start:dev"
command = "SHOULD_NOT_BE_USED"
"""
    manifest = _read({_COMMITTED_PATH: content})
    captured = capsys.readouterr()
    assert manifest.services[0].cmd == "npm run start:dev"
    assert captured.err == ""


def test_absent_cmd_defaults_to_empty_string() -> None:
    """When neither 'cmd' nor 'command' is present, cmd defaults to '' (interactive pane)."""
    content = """\
session_prefix = "mp"

[[service]]
name = "shell"
target = "1.0"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].cmd == ""


def test_empty_cmd_produces_interactive_pane() -> None:
    """cmd = '' is legal and signals an interactive shell pane."""
    content = """\
session_prefix = "mp"

[[service]]
name = "shell"
target = "1.0"
cmd = ""
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].cmd == ""


def test_overlay_command_alias_overrides_committed_cmd(capsys: pytest.CaptureFixture[str]) -> None:
    """Overlay using deprecated 'command' key overrides committed 'cmd' for the same service."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
cmd = "committed-cmd"
"""
    overlay = """\
[[service]]
name = "backend"
target = "0.0"
command = "overlay-cmd"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    captured = capsys.readouterr()
    assert manifest.services[0].cmd == "overlay-cmd"
    assert "deprecation" in captured.err
    assert "backend" in captured.err


def test_legacy_command_non_string_raises() -> None:
    """When 'command' (legacy key) is a non-string, ManifestError is raised."""
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = 42
"""
    with pytest.raises(ManifestError, match="cmd must be a string"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# port field — literal integer, offset expression, absent
# ---------------------------------------------------------------------------


def test_service_port_literal_integer_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
port = 4070
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].port == 4070


def test_service_port_offset_expression_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
port = "WINTER_PORT_BASE + 10"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].port == "WINTER_PORT_BASE + 10"


def test_service_port_absent_is_none() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].port is None


def test_service_port_bool_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
port = true
"""
    with pytest.raises(ManifestError, match="port"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# cwd field — bare relative, "./"-normalization, absent
# ---------------------------------------------------------------------------


def test_service_cwd_bare_relative_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
cwd = "apps/backend"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].cwd == "apps/backend"


def test_service_cwd_leading_dot_slash_normalized() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
cwd = "./apps/backend"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].cwd == "apps/backend"


def test_service_cwd_absent_is_none() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].cwd is None


def test_service_cwd_non_string_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
cwd = 42
"""
    with pytest.raises(ManifestError, match="cwd must be a string"):
        _read({_COMMITTED_PATH: content})


# ---------------------------------------------------------------------------
# depends_on
# ---------------------------------------------------------------------------


def test_service_depends_on_parsed() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "builder"
target = "0.0"
cmd = "npm run build"

[[service]]
name = "api"
target = "0.1"
cmd = "npm run start"
depends_on = ["builder", "workspace/db"]
"""
    manifest = _read({_COMMITTED_PATH: content})
    api = next(s for s in manifest.services if s.name == "api")
    assert api.depends_on == ("builder", "workspace/db")
    builder = next(s for s in manifest.services if s.name == "builder")
    assert builder.depends_on == ()


def test_service_depends_on_absent_is_empty_tuple() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].depends_on == ()


def test_service_depends_on_non_list_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
depends_on = "builder"
"""
    with pytest.raises(ManifestError, match="'depends_on' must be a list of strings"):
        _read({_COMMITTED_PATH: content})


def test_service_depends_on_non_string_entry_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "web"
target = "0.0"
cmd = "npm run start"
depends_on = ["builder", 42]
"""
    with pytest.raises(ManifestError, match="'depends_on' entries must be strings"):
        _read({_COMMITTED_PATH: content})
