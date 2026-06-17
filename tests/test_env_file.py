"""Tests for env parsing, interpolation, and EnvFileReader service."""

from pathlib import Path

import pytest

from service_manifest.modules.manifest.env import interpolate, parse_env_text, referenced_vars
from service_manifest.modules.manifest.env_reader import EnvFileReader
from service_manifest.modules.manifest.errors import ManifestError
from tests.fakes import FakeFilesystemReader, _conforms_fake_filesystem_reader

# ---------------------------------------------------------------------------
# parse_env_text
# ---------------------------------------------------------------------------

_SAMPLE = """\
# This is a comment
SIMPLE=value

QUOTED_DOUBLE="hello world"
QUOTED_SINGLE='another value'
export EXPORTED=exported_value
KEY_WITH_EQUALS=a=b=c
   LEADING_SPACE=trimmed
# Another comment
EMPTY_VALUE=
"""


def test_parse_skips_comments_and_blank_lines() -> None:
    result = parse_env_text(_SAMPLE)
    assert "This is a comment" not in result
    assert "Another comment" not in result
    assert "" not in result


def test_parse_simple_key_value() -> None:
    result = parse_env_text(_SAMPLE)
    assert result["SIMPLE"] == "value"


def test_parse_strips_double_quotes() -> None:
    result = parse_env_text(_SAMPLE)
    assert result["QUOTED_DOUBLE"] == "hello world"


def test_parse_strips_single_quotes() -> None:
    result = parse_env_text(_SAMPLE)
    assert result["QUOTED_SINGLE"] == "another value"


def test_parse_strips_export_prefix() -> None:
    result = parse_env_text(_SAMPLE)
    assert result["EXPORTED"] == "exported_value"


def test_parse_value_with_equals_is_preserved() -> None:
    result = parse_env_text(_SAMPLE)
    assert result["KEY_WITH_EQUALS"] == "a=b=c"


def test_parse_strips_leading_whitespace_from_key() -> None:
    result = parse_env_text(_SAMPLE)
    assert result["LEADING_SPACE"] == "trimmed"


def test_parse_empty_value() -> None:
    result = parse_env_text(_SAMPLE)
    assert result["EMPTY_VALUE"] == ""


def test_parse_empty_string_returns_empty_dict() -> None:
    assert parse_env_text("") == {}


def test_parse_only_comments_and_blanks_returns_empty_dict() -> None:
    text = "# comment\n\n# another\n"
    assert parse_env_text(text) == {}


# ---------------------------------------------------------------------------
# EnvFileReader — via FakeFilesystemReader (no real filesystem I/O)
# ---------------------------------------------------------------------------

_ENV_PATH = Path("/fake/workspace/.winter.env")


def test_env_reader_parses_correctly() -> None:
    fs = FakeFilesystemReader({_ENV_PATH: "PORT=4100\n"})
    reader = EnvFileReader(fs)
    assert reader.resolve(_ENV_PATH) == {"PORT": "4100"}


def test_env_reader_missing_file_returns_none() -> None:
    # Path declared but file absent → None ("no env file", validator skips var checks).
    fs = FakeFilesystemReader({})
    reader = EnvFileReader(fs)
    assert reader.resolve(_ENV_PATH) is None


def test_env_reader_none_path_returns_none() -> None:
    fs = FakeFilesystemReader({})
    reader = EnvFileReader(fs)
    assert reader.resolve(None) is None


def test_env_reader_present_empty_file_returns_empty_dict() -> None:
    # File present but empty → {} (distinct from absent): validator still runs
    # ${VAR} checks against an empty env.
    fs = FakeFilesystemReader({_ENV_PATH: ""})
    reader = EnvFileReader(fs)
    assert reader.resolve(_ENV_PATH) == {}


def test_env_reader_parses_all_features() -> None:
    content = "KEY=val\nexport EXPORTED=exp_val\n# comment\n\nQUOTED=\"quoted val\"\n"
    fs = FakeFilesystemReader({_ENV_PATH: content})
    reader = EnvFileReader(fs)
    result = reader.resolve(_ENV_PATH)
    assert result is not None
    assert result["KEY"] == "val"
    assert result["EXPORTED"] == "exp_val"
    assert result["QUOTED"] == "quoted val"


def test_env_reader_non_utf8_file_raises_manifest_error() -> None:
    """A non-UTF-8 env file must surface as ManifestError, not a raw UnicodeDecodeError.

    EnvFileReader.resolve now wraps UnicodeDecodeError from the filesystem reader
    in a ManifestError so callers at the CLI boundary see a clean error message
    instead of an unhandled exception.
    """

    class _BinaryFakeFilesystemReader(FakeFilesystemReader):
        """Fake whose read_text raises UnicodeDecodeError for the env path."""

        def read_text(self, path: Path) -> str:
            if path == _ENV_PATH:
                # Simulate what the real LocalFilesystemReader raises on non-UTF-8 content.
                raise UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
            return super().read_text(path)

        def exists(self, path: Path) -> bool:
            return path == _ENV_PATH

    reader = EnvFileReader(_BinaryFakeFilesystemReader())
    with pytest.raises(ManifestError, match="non-UTF-8"):
        reader.resolve(_ENV_PATH)


# ---------------------------------------------------------------------------
# FakeFilesystemReader satisfies IFilesystemReader Protocol
# ---------------------------------------------------------------------------


def test_fake_filesystem_reader_conforms_to_protocol() -> None:
    """The _conforms sentinel proves FakeFilesystemReader satisfies IFilesystemReader."""
    fake = FakeFilesystemReader()
    # If this call type-checks, the Protocol is satisfied.
    result = _conforms_fake_filesystem_reader(fake)
    assert result is fake


# ---------------------------------------------------------------------------
# interpolate
# ---------------------------------------------------------------------------


def test_interpolate_resolves_known_var() -> None:
    rendered, unresolved = interpolate("http://localhost:${PORT}", {"PORT": "4100"})
    assert rendered == "http://localhost:4100"
    assert unresolved == []


def test_interpolate_leaves_unknown_var_literal() -> None:
    rendered, unresolved = interpolate("http://localhost:${BACKEND_PORT}", {})
    assert rendered == "http://localhost:${BACKEND_PORT}"
    assert unresolved == ["BACKEND_PORT"]


def test_interpolate_mixed_resolvable_and_unresolvable() -> None:
    template = "http://${HOST}:${PORT}/api"
    env = {"HOST": "localhost"}
    rendered, unresolved = interpolate(template, env)
    assert rendered == "http://localhost:${PORT}/api"
    assert unresolved == ["PORT"]


def test_interpolate_reports_each_unresolvable_once() -> None:
    template = "${X} and ${X} again"
    rendered, unresolved = interpolate(template, {})
    assert rendered == "${X} and ${X} again"
    assert unresolved == ["X"]


def test_interpolate_does_not_expand_bare_dollar_var() -> None:
    """Bare $VAR (without braces) must NOT be interpolated."""
    rendered, unresolved = interpolate("echo $HOME", {"HOME": "/root"})
    assert rendered == "echo $HOME"
    assert unresolved == []


def test_interpolate_empty_template() -> None:
    rendered, unresolved = interpolate("", {"A": "1"})
    assert rendered == ""
    assert unresolved == []


def test_interpolate_no_placeholders() -> None:
    rendered, unresolved = interpolate("http://localhost:8080", {"PORT": "4100"})
    assert rendered == "http://localhost:8080"
    assert unresolved == []


# ---------------------------------------------------------------------------
# referenced_vars
# ---------------------------------------------------------------------------


def test_referenced_vars_empty_template() -> None:
    assert referenced_vars("") == []


def test_referenced_vars_returns_names_in_order() -> None:
    template = "${HOST}:${PORT}/path?key=${TOKEN}"
    assert referenced_vars(template) == ["HOST", "PORT", "TOKEN"]


def test_referenced_vars_deduplicates() -> None:
    template = "${X} and ${X} and ${Y}"
    assert referenced_vars(template) == ["X", "Y"]


def test_referenced_vars_ignores_bare_dollar() -> None:
    assert referenced_vars("echo $HOME") == []
