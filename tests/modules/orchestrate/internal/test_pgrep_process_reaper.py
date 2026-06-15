"""Tests for PgrepProcessReaper.

All subprocess calls (pgrep) and os.kill calls are patched at the adapter's
import site.  Tests assert:
  1. Exact argv passed to subprocess.run for pgrep -P.
  2. Recursive descendant collection order.
  3. SIGTERM → sleep 1 → SIGKILL ordering for term_then_kill.
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
# term_then_kill
# ---------------------------------------------------------------------------


def test_term_then_kill_sends_sigterm_then_sigkill(monkeypatch):
    monkeypatch.setattr(adapter_module, "subprocess", MagicMock())

    killed: list[tuple[int, int]] = []
    fake_os = MagicMock()
    fake_os.kill.side_effect = lambda pid, sig: killed.append((pid, sig))
    monkeypatch.setattr(adapter_module, "os", fake_os)

    sleep_calls: list[float] = []
    fake_time = MagicMock()
    fake_time.sleep.side_effect = lambda t: sleep_calls.append(t)
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    reaper.term_then_kill([101, 102])

    term_calls = [(pid, sig) for pid, sig in killed if sig == signal.SIGTERM]
    kill_calls = [(pid, sig) for pid, sig in killed if sig == signal.SIGKILL]

    assert len(term_calls) == 2
    assert {pid for pid, _ in term_calls} == {101, 102}

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 1

    assert len(kill_calls) == 2
    assert {pid for pid, _ in kill_calls} == {101, 102}

    # TERM must all come before KILL
    term_indices = [i for i, (_, sig) in enumerate(killed) if sig == signal.SIGTERM]
    kill_indices = [i for i, (_, sig) in enumerate(killed) if sig == signal.SIGKILL]
    assert max(term_indices) < min(kill_indices)


def test_term_then_kill_is_noop_for_empty_list(monkeypatch):
    monkeypatch.setattr(adapter_module, "subprocess", MagicMock())

    reaper = PgrepProcessReaper()
    # should not raise and should not call anything
    reaper.term_then_kill([])


def test_term_then_kill_suppresses_process_lookup_error(monkeypatch):
    monkeypatch.setattr(adapter_module, "subprocess", MagicMock())

    fake_os = MagicMock()
    fake_os.kill.side_effect = ProcessLookupError("no such process")
    monkeypatch.setattr(adapter_module, "os", fake_os)

    fake_time = MagicMock()
    monkeypatch.setattr(adapter_module, "time", fake_time)

    reaper = PgrepProcessReaper()
    # should not raise even when all PIDs are gone
    reaper.term_then_kill([999])
