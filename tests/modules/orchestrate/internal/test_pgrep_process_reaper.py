"""Tests for PgrepProcessReaper.

All subprocess calls (pgrep) and os.kill calls are patched at the adapter's
import site.  Tests assert:
  1. Exact argv passed to subprocess.run for pgrep -P.
  2. Recursive descendant collection order.
  3. SIGTERM → sleep 1 → re-collect → SIGKILL ordering for reap_descendants.
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock

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
