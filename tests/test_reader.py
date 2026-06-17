"""Tests for service_manifest.modules.manifest.reader — TOML parse, overlay merge, ManifestError paths."""

from pathlib import Path

import pytest

from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import LogConfig, LogMode, Service, ServiceManifest, StatusUrl, Target
from service_manifest.modules.manifest.reader import ManifestReader
from tests.fakes import FakeFilesystemReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MANIFEST_SUBPATH = Path("ai") / "project"
_COMMITTED_PATH = _MANIFEST_SUBPATH / "setup-tmux.toml"
_LOCAL_PATH = _MANIFEST_SUBPATH / "setup-tmux.local.toml"

_ROOT = Path("/fake/workspace")


def _reader_with(files: dict[Path, str]) -> ManifestReader:
    """Build a ManifestReader over a FakeFilesystemReader seeded with *files*.

    Keys in *files* are joined to the fake workspace root so callers can
    write relative paths like ``_COMMITTED_PATH``.
    """
    abs_files = {_ROOT / k: v for k, v in files.items()}
    return ManifestReader(FakeFilesystemReader(abs_files))


def _read(files: dict[Path, str]) -> ServiceManifest:
    return _reader_with(files).read(_ROOT)


# ---------------------------------------------------------------------------
# Valid parse — scalars, services, status urls, target parsing
# ---------------------------------------------------------------------------


def test_valid_full_manifest() -> None:
    content = """\
session_prefix = "mp"
env_file = ".winter.env"
layout_hook = "ai/project/layout-hook.sh"

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

[[status.url]]
label = "Frontend"
url = "http://localhost:${FRONTEND_PORT}"
"""
    manifest = _read({_COMMITTED_PATH: content})

    assert isinstance(manifest, ServiceManifest)
    assert manifest.session_prefix == "mp"
    assert manifest.env_file == ".winter.env"
    assert manifest.layout_hook == "ai/project/layout-hook.sh"

    assert len(manifest.services) == 3
    assert manifest.services[0] == Service(
        name="backend", target=Target(window=0, pane=0), command="npm run start:dev"
    )
    assert manifest.services[1] == Service(
        name="frontend", target=Target(window=0, pane=1), command="npm run dev"
    )
    assert manifest.services[2] == Service(
        name="shell", target=Target(window=1, pane=0), command=""
    )

    assert len(manifest.status_urls) == 2
    assert manifest.status_urls[0] == StatusUrl(
        label="Backend", url="http://localhost:${BACKEND_PORT}"
    )
    assert manifest.status_urls[1] == StatusUrl(
        label="Frontend", url="http://localhost:${FRONTEND_PORT}"
    )


def test_valid_minimal_manifest() -> None:
    """Only session_prefix is required; all other fields may be absent."""
    manifest = _read({_COMMITTED_PATH: 'session_prefix = "mp"\n'})

    assert manifest.session_prefix == "mp"
    assert manifest.env_file is None
    assert manifest.layout_hook is None
    assert manifest.services == ()
    assert manifest.status_urls == ()


def test_target_parsed_to_correct_window_and_pane() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "2.3"
command = "echo hi"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].target == Target(window=2, pane=3)


def test_missing_optional_fields_are_none() -> None:
    content = """\
session_prefix = "proj"

[[service]]
name = "worker"
target = "0.0"
command = "python -m worker"
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


def test_missing_session_prefix_raises() -> None:
    with pytest.raises(ManifestError, match="session_prefix"):
        _read({_COMMITTED_PATH: 'env_file = ".env"\n'})


def test_service_missing_name_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
target = "0.0"
command = "cmd"
"""
    with pytest.raises(ManifestError, match="name"):
        _read({_COMMITTED_PATH: content})


def test_service_missing_target_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "backend"
command = "npm start"
"""
    with pytest.raises(ManifestError, match="target"):
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
command = "cmd"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


def test_malformed_target_single_integer_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0"
command = "cmd"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


def test_malformed_target_extra_dots_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.1.2"
command = "cmd"
"""
    with pytest.raises(ManifestError, match="malformed target"):
        _read({_COMMITTED_PATH: content})


def test_malformed_target_letters_in_parts_raises() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "a.b"
command = "cmd"
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
    manifest = _read({
        _COMMITTED_PATH: 'session_prefix = "committed"\n',
        _LOCAL_PATH: 'session_prefix = "local"\n',
    })
    assert manifest.session_prefix == "local"


def test_overlay_overrides_env_file() -> None:
    committed = """\
session_prefix = "mp"
env_file = ".winter.env"
"""
    manifest = _read({
        _COMMITTED_PATH: committed,
        _LOCAL_PATH: 'env_file = ".local.env"\n',
    })
    assert manifest.env_file == ".local.env"


def test_overlay_overrides_layout_hook() -> None:
    committed = """\
session_prefix = "mp"
layout_hook = "ai/project/layout-hook.sh"
"""
    manifest = _read({
        _COMMITTED_PATH: committed,
        _LOCAL_PATH: 'layout_hook = "ai/project/layout-hook.local.sh"\n',
    })
    assert manifest.layout_hook == "ai/project/layout-hook.local.sh"


def test_overlay_absent_scalar_uses_committed() -> None:
    committed = """\
session_prefix = "mp"
env_file = ".winter.env"
"""
    manifest = _read({
        _COMMITTED_PATH: committed,
        _LOCAL_PATH: 'session_prefix = "local"\n',
    })
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
command = "npm run start:dev"
"""
    overlay = """\
[[service]]
name = "backend"
target = "0.0"
command = "npm run start:prod"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.services) == 1
    assert manifest.services[0].command == "npm run start:prod"


def test_overlay_service_overrides_target_field() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = "cmd"
"""
    overlay = """\
[[service]]
name = "backend"
target = "1.0"
command = "cmd"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert manifest.services[0].target == Target(window=1, pane=0)


# ---------------------------------------------------------------------------
# Overlay semantics — service append (new name)
# ---------------------------------------------------------------------------


def test_overlay_service_append_new_name() -> None:
    committed = """\
session_prefix = "mp"

[[service]]
name = "backend"
target = "0.0"
command = "npm run start:dev"
"""
    overlay = """\
[[service]]
name = "worker"
target = "1.0"
command = "python -m worker"
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
command = "backend-cmd"

[[service]]
name = "frontend"
target = "0.1"
command = "frontend-cmd"
"""
    overlay = """\
[[service]]
name = "frontend"
target = "0.1"
command = "frontend-override"

[[service]]
name = "shell"
target = "1.0"
command = ""
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.services) == 3
    # backend unchanged at position 0
    assert manifest.services[0].name == "backend"
    assert manifest.services[0].command == "backend-cmd"
    # frontend overridden in place at position 1
    assert manifest.services[1].name == "frontend"
    assert manifest.services[1].command == "frontend-override"
    # shell appended at position 2
    assert manifest.services[2].name == "shell"


# ---------------------------------------------------------------------------
# Overlay semantics — status.url override + append
# ---------------------------------------------------------------------------


def test_overlay_status_url_override_by_label() -> None:
    committed = """\
session_prefix = "mp"

[[status.url]]
label = "Backend"
url = "http://localhost:3000"
"""
    overlay = """\
[[status.url]]
label = "Backend"
url = "http://localhost:4100"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.status_urls) == 1
    assert manifest.status_urls[0].url == "http://localhost:4100"


def test_overlay_status_url_append_new_label() -> None:
    committed = """\
session_prefix = "mp"

[[status.url]]
label = "Backend"
url = "http://localhost:3000"
"""
    overlay = """\
[[status.url]]
label = "Frontend"
url = "http://localhost:3001"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.status_urls) == 2
    assert manifest.status_urls[0].label == "Backend"
    assert manifest.status_urls[1].label == "Frontend"


def test_overlay_status_url_override_and_append_preserves_order() -> None:
    committed = """\
session_prefix = "mp"

[[status.url]]
label = "Backend"
url = "http://localhost:3000"

[[status.url]]
label = "Frontend"
url = "http://localhost:3001"
"""
    overlay = """\
[[status.url]]
label = "Frontend"
url = "http://localhost:4200"

[[status.url]]
label = "Docs"
url = "http://localhost:4300"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: overlay})
    assert len(manifest.status_urls) == 3
    assert manifest.status_urls[0].label == "Backend"
    assert manifest.status_urls[0].url == "http://localhost:3000"
    assert manifest.status_urls[1].label == "Frontend"
    assert manifest.status_urls[1].url == "http://localhost:4200"
    assert manifest.status_urls[2].label == "Docs"
    assert manifest.status_urls[2].url == "http://localhost:4300"


# ---------------------------------------------------------------------------
# Malformed overlay TOML raises ManifestError
# ---------------------------------------------------------------------------


def test_malformed_overlay_toml_raises() -> None:
    with pytest.raises(ManifestError, match="malformed TOML"):
        _read({
            _COMMITTED_PATH: 'session_prefix = "mp"\n',
            _LOCAL_PATH: "this is [ not valid toml !!!\n",
        })


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
command = "cmd"
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
command = "cmd"
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
command = "cmd"
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
    assert manifest.logs == LogConfig(
        rotate_size_bytes=2048, max_rotations=3, retention_seconds=86400
    )


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
command = "npm run watch"
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
command = "npm run start:dev"
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
command = "cmd"
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
command = "npm start"
"""
    manifest = _read({_COMMITTED_PATH: content})
    assert manifest.services[0].log == LogMode.FILE


def test_service_log_invalid_string_raises_naming_allowed_values() -> None:
    content = """\
session_prefix = "mp"

[[service]]
name = "svc"
target = "0.0"
command = "cmd"
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
command = "cmd"
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
command = "npm start"
log = "pane"
"""
    # Local overlay changes command only — log="pane" should be preserved.
    local = """\
[[service]]
name = "backend"
command = "npm run start:debug"
"""
    manifest = _read({_COMMITTED_PATH: committed, _LOCAL_PATH: local})
    assert manifest.services[0].command == "npm run start:debug"
    assert manifest.services[0].log == LogMode.PANE


def test_overlay_service_can_override_log_to_file() -> None:
    """Local overlay can change log mode for a committed service."""
    committed = """\
session_prefix = "mp"

[[service]]
name = "watcher"
target = "0.0"
command = "npm run watch"
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
# Overlay merge does NOT mutate the committed dict (AC: shallow-copy aliasing)
# ---------------------------------------------------------------------------


def test_overlay_merge_does_not_mutate_committed_status_url() -> None:
    """Merging a local overlay into a committed doc with [[status.url]] must not
    mutate the committed dict's nested 'status' table.

    The prior bug: ``result = dict(committed)`` is shallow, so
    ``result.setdefault('status', {})['url'] = merged_urls`` would write back
    into committed's original 'status' dict via the aliased reference.  The fix
    builds a fresh dict: ``result['status'] = {**committed.get('status', {}),
    'url': merged_urls}``.
    """
    from service_manifest.modules.manifest.reader import ManifestReader

    committed_toml = """\
session_prefix = "mp"

[[status.url]]
label = "Backend"
url = "http://localhost:3000"
"""
    overlay_toml = """\
[[status.url]]
label = "Backend"
url = "http://localhost:4100"
"""
    abs_committed = _ROOT / _COMMITTED_PATH
    abs_local = _ROOT / _LOCAL_PATH
    fake_fs = FakeFilesystemReader({abs_committed: committed_toml, abs_local: overlay_toml})
    reader = ManifestReader(fake_fs)

    # Parse the committed dict manually so we can inspect it after merge.
    import tomllib

    committed_doc = tomllib.loads(committed_toml)
    original_url = committed_doc["status"]["url"][0]["url"]
    assert original_url == "http://localhost:3000"

    # Invoke reader.read() — internally calls _merge(committed, local).
    reader.read(_ROOT)

    # The committed_doc must be unchanged — no aliased mutation.
    assert committed_doc["status"]["url"][0]["url"] == "http://localhost:3000", (
        "overlay merge mutated the committed dict's nested 'status' table"
    )
