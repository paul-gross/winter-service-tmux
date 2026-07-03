"""Readiness probe seam.

``IHealthChecker`` abstracts command, network, log-scan, and uptime probes so
the orchestrator service can populate status health without owning subprocess,
HTTP, log/pane, or process-table I/O itself.
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
        uptime_seconds: int | None = None,
    ) -> bool:
        """Return ``True`` when *health* passes, otherwise ``False``.

        *log_source* carries the already-captured output text for a
        ``HealthType.LOG`` probe — a bounded log-file tail or a tmux pane
        capture, fetched by the caller (which owns those repositories).
        Ignored by every other probe type.

        *uptime_seconds* carries the already-measured elapsed seconds of the
        service's pane-child process for a ``HealthType.UPTIME`` probe — the
        caller (which owns the tmux pane PID and the process reaper) resolves
        the child and its uptime; this seam never receives a PID. ``None``
        means no child process was found (interactive pane, or the process
        exited), which is unhealthy. Ignored by every other probe type.
        """
        ...
