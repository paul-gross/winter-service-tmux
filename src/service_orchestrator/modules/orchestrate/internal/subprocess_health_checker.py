"""Stdlib health-check adapter for URL and shell-command probes."""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from service_manifest.modules.manifest.env import interpolate
from service_manifest.modules.manifest.model import Health, HealthType

_DEFAULT_TIMEOUT_SECONDS = 5.0


class SubprocessHealthChecker:
    """Run declared readiness probes using subprocesses / urllib."""

    def is_healthy(self, health: Health, env: dict[str, str] | None = None, cwd: Path | None = None) -> bool:
        target, unresolved = interpolate(health.target, env or {})
        if unresolved:
            return False

        timeout = health.timeout if health.timeout is not None else _DEFAULT_TIMEOUT_SECONDS
        if health.type == HealthType.URL:
            return self._check_url(target, timeout)
        if health.type == HealthType.CMD:
            return self._check_cmd(target, timeout, cwd)
        return False

    @staticmethod
    def _check_url(target: str, timeout: float) -> bool:
        try:
            with urllib.request.urlopen(target, timeout=timeout) as response:
                return 200 <= response.status < 400
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            return False

    @staticmethod
    def _check_cmd(target: str, timeout: float, cwd: Path | None) -> bool:
        try:
            result = subprocess.run(
                target,
                shell=True,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                cwd=cwd,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0
