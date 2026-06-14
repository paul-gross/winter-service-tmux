"""Tests for service_manifest.modules.manifest.reader — TOML parse, overlay merge, ManifestError paths."""

from pathlib import Path

import pytest

from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import Service, ServiceManifest, StatusUrl, Target
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
