"""Readiness probe seam.

``IHealthChecker`` abstracts command, network, and log-scan probes so the
orchestrator service can populate status health without owning subprocess,
HTTP, or log/pane I/O itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from service_manifest.modules.manifest.model import Health


class IHealthChecker(Protocol):
    """Execute a declared readiness probe."""

    def is_healthy(
        self,
        health: Health,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        log_source: str | None = None,
    ) -> bool:
        """Return ``True`` when *health* passes, otherwise ``False``.

        *log_source* carries the already-captured output text for a
        ``HealthType.LOG`` probe — a bounded log-file tail or a tmux pane
        capture, fetched by the caller (which owns those repositories).
        Ignored by every other probe type.
        """
        ...
