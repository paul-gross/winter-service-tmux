"""Composition root — wires ``IFilesystemReader`` to concrete services.

Plain hand-written DI: no framework, no third-party library.  Accepting an
optional ``fs`` override lets callers (and tests) inject a fake or stub
without touching the real filesystem.
"""

from __future__ import annotations

from service_manifest.core.filesystem import IFilesystemReader
from service_manifest.core.internal.local_filesystem import LocalFilesystemReader
from service_manifest.modules.manifest.env_reader import EnvFileReader
from service_manifest.modules.manifest.reader import ManifestReader
from service_manifest.modules.manifest.validator import ManifestValidator


class Container:
    """Composition root: constructs adapters and injects them into services."""

    def __init__(self, fs: IFilesystemReader | None = None) -> None:
        self.filesystem: IFilesystemReader = fs or LocalFilesystemReader()
        self.manifest_reader = ManifestReader(self.filesystem)
        self.env_reader = EnvFileReader(self.filesystem)
        self.validator = ManifestValidator()
