"""Readiness probe seam.

``IHealthChecker`` abstracts command and network probes so the orchestrator
service can populate status health without owning subprocess or HTTP I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from service_manifest.modules.manifest.model import Health


class IHealthChecker(Protocol):
    """Execute a declared readiness probe."""

    def is_healthy(self, health: Health, env: dict[str, str] | None = None, cwd: Path | None = None) -> bool:
        """Return ``True`` when *health* passes, otherwise ``False``."""
        ...
