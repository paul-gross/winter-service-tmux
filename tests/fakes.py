"""In-memory test doubles for IFilesystemReader.

FakeFilesystemReader lets reader/env tests inject pre-loaded content
without touching the real filesystem.  Existence is derived from the
content dict: a path exists iff it is a key in the dict.
"""

from __future__ import annotations

from pathlib import Path

from service_manifest.core.filesystem import IFilesystemReader


class FakeFilesystemReader:
    """In-memory IFilesystemReader backed by a ``dict[Path, str]``.

    A path is considered to exist iff it is present as a key.
    ``read_text`` raises ``FileNotFoundError`` for absent paths (mirrors
    ``pathlib.Path.read_text`` on a missing file).
    """

    def __init__(self, files: dict[Path, str] | None = None) -> None:
        self._files: dict[Path, str] = files or {}

    def exists(self, path: Path) -> bool:
        return path in self._files

    def read_text(self, path: Path) -> str:
        if path not in self._files:
            raise FileNotFoundError(f"FakeFilesystemReader: no such file: {path}")
        return self._files[path]


def _conforms_fake_filesystem_reader(x: FakeFilesystemReader) -> IFilesystemReader:
    """Typecheck-time sentinel: FakeFilesystemReader satisfies IFilesystemReader."""
    return x
