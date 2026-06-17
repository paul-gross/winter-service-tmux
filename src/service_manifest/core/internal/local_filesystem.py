from __future__ import annotations

from pathlib import Path

from service_manifest.core.filesystem import IFilesystemReader


class LocalFilesystemReader:
    """Local-disk adapter for IFilesystemReader.

    All direct pathlib filesystem access is confined here so services depend on
    the Protocol rather than reaching the standard library. Methods are thin
    pass-throughs — orchestration stays in services.
    """

    @staticmethod
    def exists(path: Path) -> bool:
        return path.exists()

    @staticmethod
    def read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="strict")


def _conforms_local_filesystem_reader(x: LocalFilesystemReader) -> IFilesystemReader:
    return x
