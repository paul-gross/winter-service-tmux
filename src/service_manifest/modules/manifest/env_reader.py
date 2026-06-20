"""EnvFileReader — resolves a ``.env`` file path to a parsed env dict.

A thin service wrapping ``parse_env_text`` with filesystem access via
``IFilesystemReader``.  A None or absent path resolves to ``None`` ("no env
file" — the validator then skips ``${VAR}`` checks, mirroring the bash ``up``
script's warn-and-continue for a missing env file); a present file resolves to a
(possibly empty) dict.  Owning this absent-vs-present distinction here keeps the
CLI handler from reaching back into the filesystem seam to re-derive it.
"""

from __future__ import annotations

from pathlib import Path

from service_manifest.core.filesystem import IFilesystemReader
from service_manifest.modules.manifest.env import parse_env_text
from service_manifest.modules.manifest.errors import ManifestError


class EnvFileReader:
    """Resolves a ``.env`` file path to a ``{key: value}`` mapping.

    Injected with ``IFilesystemReader`` so tests can feed in-memory content
    without touching the real filesystem.
    """

    def __init__(self, fs: IFilesystemReader) -> None:
        self._fs = fs

    def resolve(self, env_file_path: Path | None) -> dict[str, str] | None:
        """Return the parsed env dict for *env_file_path*, or ``None``.

        Returns ``None`` when *env_file_path* is ``None`` or the path does not
        exist on the filesystem — signalling "no env file" so the validator
        skips ``${VAR}`` checks; a missing env file is not an error.  Returns a
        (possibly empty) dict when the file is present.
        """
        if env_file_path is None or not self._fs.exists(env_file_path):
            return None
        try:
            text = self._fs.read_text(env_file_path)
        except UnicodeDecodeError as exc:
            raise ManifestError(f"env file '{env_file_path}' contains non-UTF-8 bytes: {exc}") from exc
        return parse_env_text(text)
