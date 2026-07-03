"""Tests for PgrepProcessReaper.

All subprocess calls (pgrep) and os.kill calls are patched at the adapter's
import site.  Tests assert:
  1. Exact argv passed to subprocess.run for pgrep -P.
  2. Recursive descendant collection order.
  3. SIGTERM → sleep 1 → re-collect → SIGKILL ordering for reap_descendants.
  4. Non-ProcessLookupError signal failures emit a logger.warning.
  5. Missing pgrep binary raises OrchestratorError.
"""

from __future__ import annotations

import logging
import signal
from unittest.mock import MagicMock

import pytest

from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.internal import pgrep_process_reaper as adapter_module
from service_orchestrator.modules.orchestrate.internal.pgrep_process_reaper import PgrepProcessReaper


def _pgrep_result(returncode: int = 0, stdout: str = "") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = ""
    mock.args = []
    return mock


# ---------------------------------------------------------------------------
# descendants
# ---------------------------------------------------------------------------


def test_descendants_calls_pgrep_with_pid(monkeypatch):
    fake_subprocess = MagicMock()
    # pid 100 has no children
    fake_subprocess.run.return_value = _pgrep_result(returncode=1, stdout="")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    result = reaper.descendants(100)

    fake_subprocess.run.assert_called_once_with(
        ["pgrep", "-P", "100"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result == []


def test_descendants_excludes_root_pid(monkeypatch):
    # root=100, children=[101], grandchildren of 101=[]
    call_results = {
        "100": _pgrep_result(returncode=0, stdout="101\n"),
        "101": _pgrep_result(returncode=1, stdout=""),
    }

    def fake_run(args, **kwargs):
        pid_arg = args[2]
        return call_results[pid_arg]

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    result = reaper.descendants(100)

    # root (100) excluded; child 101 included
    assert result == [101]


def test_descendants_walks_tree_recursively(monkeypatch):
    # Tree: 100 → [101, 102], 101 → [103], 102 → [], 103 → []
    call_results = {
        "100": _pgrep_result(returncode=0, stdout="101\n102\n"),
        "101": _pgrep_result(returncode=0, stdout="103\n"),
        "102": _pgrep_result(returncode=1, stdout=""),
        "103": _pgrep_result(returncode=1, stdout=""),
    }

    def fake_run(args, **kwargs):
        pid_arg = args[2]
        return call_results[pid_arg]

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    result = reaper.descendants(100)

    # Root 100 excluded; 101, 102, 103 included (post-order: 103 before 101, then 102)
    assert set(result) == {101, 102, 103}
    assert 100 not in result


def test_descendants_returns_empty_for_leaf(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _pgrep_result(returncode=1, stdout="")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    assert reaper.descendants(999) == []


# ---------------------------------------------------------------------------
# has_children
# ---------------------------------------------------------------------------


def test_has_children_returns_true_when_children_exist(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _pgrep_result(returncode=0, stdout="201\n")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    assert reaper.has_children(200) is True

    fake_subprocess.run.assert_called_once_with(
        ["pgrep", "-P", "200"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_has_children_returns_false_when_no_children(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _pgrep_result(returncode=1, stdout="")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    assert reaper.has_children(200) is False


# ---------------------------------------------------------------------------
# reap_descendants — re-collection after sleep catches grandchildren spawned
# during the shutdown window
# ---------------------------------------------------------------------------


def test_reap_descendants_re_collects_after_sleep_and_kills_new_grandchild(monkeypatch):
    """A grandchild forked between the first collection and the sleep must be
    SIGKILLed.

    Setup:
      root pane PID = 10
      Before sleep:  descendants(10) = [100]   (child 100 exists)
      After sleep:   descendants(10) = [101]   (100 died; new grandchild 101 appeared)

    The old term_then_kill-on-a-snapshot approach would SIGKILL only {100} and
    miss 101.  reap_descendants re-collects after the sleep, so 101 is SIGKILLed.
    """
    # Two successive pgrep trees: first call returns [100], second returns [101].
    call_count = {"n": 0}

    def fake_run(args, **kwargs):
        pid_arg = args[2]
        if pid_arg == "10":
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First descendants() call (before TERM): child 100 present.
                mock = _pgrep_result(returncode=0, stdout="100\n")
            else:
                # Second descendants() call (after sleep): grandchild 101 appeared.
                mock = _pgrep_result(returncode=0, stdout="101\n")
            return mock
        elif pid_arg in ("100", "101"):
            # Both children are leaves.
            return _pgrep_result(returncode=1, stdout="")
        return _pgrep_result(returncode=1, stdout="")

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    killed: list[tuple[int, int]] = []
    fake_os = MagicMock()
    fake_os.kill.side_effect = lambda pid, sig: killed.append((pid, sig))
    monkeypatch.setattr(adapter_module, "os", fake_os)

    fake_time = MagicMock()
    sleep_calls: list[float] = []
    fake_time.sleep.side_effect = lambda t: sleep_calls.append(t)
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    reaper.reap_descendants([10])

    # TERM phase must have signalled PID 100 (first-pass child).
    term_pids = {pid for pid, sig in killed if sig == signal.SIGTERM}
    assert 100 in term_pids

    # Sleep must have occurred once.
    assert len(sleep_calls) == 1

    # KILL phase must have signalled PID 101 (re-collected grandchild).
    kill_pids = {pid for pid, sig in killed if sig == signal.SIGKILL}
    assert 101 in kill_pids, (
        "grandchild 101 forked during shutdown window was not SIGKILLed — re-collection after sleep is required"
    )


def test_reap_descendants_noop_when_no_root_pids(monkeypatch):
    """reap_descendants([]) must not call pgrep, sleep, or kill."""
    fake_subprocess = MagicMock()
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    fake_os = MagicMock()
    monkeypatch.setattr(adapter_module, "os", fake_os)

    fake_time = MagicMock()
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    reaper.reap_descendants([])

    fake_subprocess.run.assert_not_called()
    fake_os.kill.assert_not_called()
    fake_time.sleep.assert_not_called()


def test_reap_descendants_term_before_sleep_before_kill(monkeypatch):
    """Ordering: all SIGTERMs → sleep → all SIGKILLs."""
    call_count = {"n": 0}

    def fake_run(args, **kwargs):
        pid_arg = args[2]
        if pid_arg == "10":
            call_count["n"] += 1
            return _pgrep_result(returncode=0, stdout="100\n")
        return _pgrep_result(returncode=1, stdout="")

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    events: list[tuple[str, object]] = []
    fake_os = MagicMock()
    fake_os.kill.side_effect = lambda pid, sig: events.append(("kill", int(sig)))
    monkeypatch.setattr(adapter_module, "os", fake_os)

    fake_time = MagicMock()
    fake_time.sleep.side_effect = lambda t: events.append(("sleep", t))
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    reaper.reap_descendants([10])

    term_idx = next(i for i, (kind, val) in enumerate(events) if kind == "kill" and val == signal.SIGTERM)
    sleep_idx = next(i for i, (kind, _) in enumerate(events) if kind == "sleep")
    kill_idx = next(i for i, (kind, val) in enumerate(events) if kind == "kill" and val == signal.SIGKILL)

    assert term_idx < sleep_idx < kill_idx


# ---------------------------------------------------------------------------
# reap_descendants — non-ProcessLookupError signal failures emit a warning
# ---------------------------------------------------------------------------


def test_reap_descendants_permission_error_sigterm_emits_warning(monkeypatch, caplog):
    """PermissionError (EPERM) during SIGTERM is warned via logger, not silently swallowed.

    The process survives because we cannot signal it (it was re-parented after
    collection).  The reaper must emit a visible diagnostic rather than passing
    silently, so operators know a stuck process may exist.
    """

    def fake_run(args, **kwargs):
        pid_arg = args[2]
        if pid_arg == "10":
            return _pgrep_result(returncode=0, stdout="100\n")
        return _pgrep_result(returncode=1, stdout="")

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    permission_err = PermissionError(1, "Operation not permitted")

    fake_os = MagicMock()
    fake_os.kill.side_effect = permission_err
    monkeypatch.setattr(adapter_module, "os", fake_os)

    fake_time = MagicMock()
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    with caplog.at_level(logging.WARNING, logger=adapter_module.__name__):
        reaper.reap_descendants([10])

    assert any("100" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_reap_descendants_permission_error_sigkill_emits_warning(monkeypatch, caplog):
    """PermissionError (EPERM) during SIGKILL phase is warned via logger.

    The SIGTERM phase succeeds (ProcessLookupError, already dead); the
    second-pass re-collection still finds pid 100; SIGKILL then raises EPERM.
    The reaper must warn rather than silently pass.
    """
    call_count = {"n": 0}

    def fake_run(args, **kwargs):
        pid_arg = args[2]
        if pid_arg == "10":
            call_count["n"] += 1
            return _pgrep_result(returncode=0, stdout="100\n")
        return _pgrep_result(returncode=1, stdout="")

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    # SIGTERM raises ProcessLookupError (already dead); SIGKILL raises PermissionError.
    permission_err = PermissionError(1, "Operation not permitted")

    def _kill(pid, sig):
        if sig == signal.SIGTERM:
            raise ProcessLookupError(3, "No such process")
        raise permission_err

    fake_os = MagicMock()
    fake_os.kill.side_effect = _kill
    monkeypatch.setattr(adapter_module, "os", fake_os)

    fake_time = MagicMock()
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    with caplog.at_level(logging.WARNING, logger=adapter_module.__name__):
        reaper.reap_descendants([10])

    assert any("100" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_reap_descendants_process_lookup_error_not_warned(monkeypatch, caplog):
    """ProcessLookupError (ESRCH) — process already exited — must NOT emit any warning.

    This is the expected case: the process died on its own between the collection
    snapshot and the signal call.  Emitting a warning here would be a false alarm.
    """

    def fake_run(args, **kwargs):
        pid_arg = args[2]
        if pid_arg == "10":
            return _pgrep_result(returncode=0, stdout="100\n")
        return _pgrep_result(returncode=1, stdout="")

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    fake_os = MagicMock()
    fake_os.kill.side_effect = ProcessLookupError(3, "No such process")
    monkeypatch.setattr(adapter_module, "os", fake_os)

    fake_time = MagicMock()
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    with caplog.at_level(logging.WARNING, logger=adapter_module.__name__):
        reaper.reap_descendants([10])

    # ProcessLookupError is silent — no warning log records expected.
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# _collect / descendants — missing pgrep binary raises OrchestratorError
# ---------------------------------------------------------------------------


def test_descendants_raises_orchestrator_error_when_pgrep_missing(monkeypatch):
    """FileNotFoundError from subprocess.run (pgrep not on PATH) is wrapped into
    OrchestratorError so the env_cli boundary can surface it as a user-facing error.
    """
    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    with pytest.raises(OrchestratorError, match="pgrep not found"):
        reaper.descendants(100)


def test_has_children_raises_orchestrator_error_when_pgrep_missing(monkeypatch):
    """FileNotFoundError from subprocess.run in has_children is wrapped into
    OrchestratorError, the same as in descendants/_collect.
    """
    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    with pytest.raises(OrchestratorError, match="pgrep not found"):
        reaper.has_children(100)


# ---------------------------------------------------------------------------
# child_uptime_seconds — used by the 'uptime' health probe
# ---------------------------------------------------------------------------


def _ps_result(returncode: int = 0, stdout: str = "") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = ""
    mock.args = []
    return mock


def test_child_uptime_seconds_returns_none_when_no_children(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = _pgrep_result(returncode=1, stdout="")
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    assert reaper.child_uptime_seconds(200) is None
    # No 'ps' call is made when there are no children.
    fake_subprocess.run.assert_called_once_with(
        ["pgrep", "-P", "200"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_child_uptime_seconds_single_child_reads_etimes(monkeypatch):
    call_results = {
        ("pgrep", "-P", "200"): _pgrep_result(returncode=0, stdout="201\n"),
        ("ps", "-o", "etimes=", "-p", "201"): _ps_result(returncode=0, stdout="  42\n"),
    }

    def fake_run(args, **kwargs):
        return call_results[tuple(args)]

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    assert reaper.child_uptime_seconds(200) == 42


def test_child_uptime_seconds_multiple_children_returns_longest_running(monkeypatch):
    """When several direct children exist, the longest-running one's uptime wins."""
    call_results = {
        ("pgrep", "-P", "200"): _pgrep_result(returncode=0, stdout="201\n202\n"),
        ("ps", "-o", "etimes=", "-p", "201"): _ps_result(returncode=0, stdout="10\n"),
        ("ps", "-o", "etimes=", "-p", "202"): _ps_result(returncode=0, stdout="99\n"),
    }

    def fake_run(args, **kwargs):
        return call_results[tuple(args)]

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    assert reaper.child_uptime_seconds(200) == 99


def test_child_uptime_seconds_returns_none_when_child_exited_before_ps_runs(monkeypatch):
    """A child that races to exit between pgrep and ps yields empty ps output."""
    call_results = {
        ("pgrep", "-P", "200"): _pgrep_result(returncode=0, stdout="201\n"),
        ("ps", "-o", "etimes=", "-p", "201"): _ps_result(returncode=1, stdout=""),
    }

    def fake_run(args, **kwargs):
        return call_results[tuple(args)]

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    assert reaper.child_uptime_seconds(200) is None


def test_child_uptime_seconds_raises_orchestrator_error_when_ps_missing(monkeypatch):
    """FileNotFoundError from subprocess.run (ps not on PATH) is wrapped into OrchestratorError."""

    def fake_run(args, **kwargs):
        if args[0] == "pgrep":
            return _pgrep_result(returncode=0, stdout="201\n")
        raise FileNotFoundError(2, "No such file or directory")

    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = fake_run
    monkeypatch.setattr(adapter_module, "subprocess", fake_subprocess)

    reaper = PgrepProcessReaper()
    with pytest.raises(OrchestratorError, match="ps not found"):
        reaper.child_uptime_seconds(200)
