"""Stdlib health-check adapter for URL, shell-command, log-scan, and uptime probes."""

from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from service_manifest.modules.manifest.env import interpolate
from service_manifest.modules.manifest.model import Health, HealthType, parse_uptime_duration

_DEFAULT_TIMEOUT_SECONDS = 5.0


class SubprocessHealthChecker:
    """Run declared readiness probes using subprocesses / urllib / regex."""

    def is_healthy(
        self,
        health: Health,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        log_source: str | None = None,
        uptime_seconds: int | None = None,
    ) -> bool:
        if health.type == HealthType.LOG:
            # 'log' targets are used VERBATIM — no ${VAR} interpolation — so
            # regex syntax (e.g. '${2}') is never mangled by substitution.
            return self._check_log(health.target, log_source)
        if health.type == HealthType.UPTIME:
            # 'uptime' targets are a plain duration, never interpolated (the
            # duration regex has no room for a '${VAR}' placeholder anyway).
            # The measured elapsed seconds are computed by the caller — this
            # seam never touches a PID or the process table itself.
            return self._check_uptime(health.target, uptime_seconds)

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

    @staticmethod
    def _check_log(pattern: str, log_source: str | None) -> bool:
        if log_source is None:
            return False
        try:
            return re.search(pattern, log_source) is not None
        except re.error:
            # An invalid regex is a manifest-validation error (caught by
            # ManifestValidator before the probe ever runs); this guards the
            # probe path against crashing if that check was somehow bypassed.
            return False

    @staticmethod
    def _check_uptime(target: str, uptime_seconds: int | None) -> bool:
        if uptime_seconds is None:
            # No child process was found (interactive pane, or the measured
            # process already exited) — the last-resort readiness signal has
            # nothing to measure, so it reports unhealthy.
            return False
        threshold = parse_uptime_duration(target)
        if threshold is None:
            # An invalid duration is a manifest-validation error (caught by
            # ManifestValidator before the probe ever runs); this guards the
            # probe path the same way an invalid 'log' regex does above.
            return False
        return uptime_seconds >= threshold
