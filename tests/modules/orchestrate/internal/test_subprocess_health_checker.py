from __future__ import annotations

import subprocess
from pathlib import Path

from service_manifest.modules.manifest.model import Health, HealthType
from service_orchestrator.modules.orchestrate.internal.subprocess_health_checker import SubprocessHealthChecker


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def test_url_health_success_status_is_healthy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    def _urlopen(url: str, timeout: float) -> _FakeResponse:
        calls.append((url, timeout))
        return _FakeResponse(204)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    checker = SubprocessHealthChecker()

    assert checker.is_healthy(
        Health(type=HealthType.URL, target="http://localhost:${PORT}/health", timeout=1),
        {"PORT": "3000"},
    )
    assert calls == [("http://localhost:3000/health", 1.0)]


def test_url_health_failure_status_is_unhealthy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _urlopen(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse(500)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(Health(type=HealthType.URL, target="http://localhost/health"))


def test_cmd_health_exit_zero_is_healthy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    def _run(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr("subprocess.run", _run)
    checker = SubprocessHealthChecker()

    cwd = Path("/fake/worktree")
    assert checker.is_healthy(Health(type=HealthType.CMD, target="pgrep -f worker", timeout=2), cwd=cwd)
    assert calls[0][0] == ("pgrep -f worker",)
    assert calls[0][1]["shell"] is True
    assert calls[0][1]["timeout"] == 2.0
    assert calls[0][1]["cwd"] == cwd


def test_cmd_health_nonzero_is_unhealthy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _run(*args, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(args=args, returncode=1)

    monkeypatch.setattr("subprocess.run", _run)
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(Health(type=HealthType.CMD, target="false"))


def test_unresolved_variable_is_unhealthy_without_running_probe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = False

    def _run(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr("subprocess.run", _run)
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(Health(type=HealthType.CMD, target="check ${MISSING}"), {})
    assert called is False


def test_log_health_pattern_matched_is_healthy() -> None:
    checker = SubprocessHealthChecker()

    assert checker.is_healthy(
        Health(type=HealthType.LOG, target=r"Listening on port \d+"),
        log_source="booting...\nListening on port 3000\n",
    )


def test_log_health_pattern_not_present_is_unhealthy() -> None:
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(
        Health(type=HealthType.LOG, target=r"Listening on port \d+"),
        log_source="booting...\nstill starting\n",
    )


def test_log_health_missing_log_source_is_unhealthy() -> None:
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(Health(type=HealthType.LOG, target="ready"), log_source=None)


def test_log_health_target_is_used_verbatim_no_var_interpolation() -> None:
    """A '${VAR}' inside a log regex is matched literally, never interpolated."""
    checker = SubprocessHealthChecker()

    assert checker.is_healthy(
        Health(type=HealthType.LOG, target=r"price: \$\{2\}"),
        env={"2": "should-not-be-substituted"},
        log_source="price: ${2}\n",
    )


def test_log_health_invalid_regex_is_unhealthy_not_a_crash() -> None:
    """An invalid regex must not crash the probe (validation catches it earlier)."""
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(
        Health(type=HealthType.LOG, target="unclosed("),
        log_source="anything",
    )


def test_uptime_health_below_threshold_is_unhealthy() -> None:
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(Health(type=HealthType.UPTIME, target="30s"), uptime_seconds=29)


def test_uptime_health_at_threshold_is_healthy() -> None:
    checker = SubprocessHealthChecker()

    assert checker.is_healthy(Health(type=HealthType.UPTIME, target="30s"), uptime_seconds=30)


def test_uptime_health_above_threshold_is_healthy() -> None:
    checker = SubprocessHealthChecker()

    assert checker.is_healthy(Health(type=HealthType.UPTIME, target="30s"), uptime_seconds=120)


def test_uptime_health_no_measured_process_is_unhealthy() -> None:
    """uptime_seconds=None means no child process was found — unhealthy."""
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(Health(type=HealthType.UPTIME, target="30s"), uptime_seconds=None)


def test_uptime_health_invalid_duration_is_unhealthy_not_a_crash() -> None:
    """An invalid duration must not crash the probe (validation catches it earlier)."""
    checker = SubprocessHealthChecker()

    assert not checker.is_healthy(Health(type=HealthType.UPTIME, target="not-a-duration"), uptime_seconds=999)
