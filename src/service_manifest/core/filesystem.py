"""Cross-cutting filesystem read seam for the service_manifest package.

Deliberately hoisted into ``core/`` ahead of the module-layout "wait for the
second consumer" rule: a filesystem reader is inherently cross-cutting, and a
second consumer (the planned tmux orchestrator that will use this package's
reader/validator) is anticipated.  Placing it here now avoids a later move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class IFilesystemReader(Protocol):
    """Read-only filesystem queries used by the service_manifest package."""

    def exists(self, path: Path) -> bool: ...
    def read_text(self, path: Path) -> str: ...
