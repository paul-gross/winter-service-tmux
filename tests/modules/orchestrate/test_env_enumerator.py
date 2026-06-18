"""Tests for service_orchestrator.modules.orchestrate.env_enumerator.

Covers:
- Sessions matching ``<prefix>-`` are returned with prefix stripped
- Sessions not matching the prefix are excluded
- Empty session list returns empty list
- Prefix with no matching sessions returns empty list
- Multiple envs are all returned
"""

from __future__ import annotations

from service_orchestrator.modules.orchestrate.env_enumerator import running_envs
from tests.conftest import FakeTmuxRepository

# ---------------------------------------------------------------------------
# Basic prefix filtering and stripping
# ---------------------------------------------------------------------------


def test_matching_sessions_are_returned_stripped() -> None:
    tmux = FakeTmuxRepository(
        sessions={
            "mp-alpha": {},
            "mp-beta": {},
        }
    )
    result = running_envs(tmux, "mp")
    assert sorted(result) == ["alpha", "beta"]


def test_non_matching_sessions_are_excluded() -> None:
    tmux = FakeTmuxRepository(
        sessions={
            "mp-alpha": {},
            "other-session": {},
            "unrelated": {},
        }
    )
    result = running_envs(tmux, "mp")
    assert result == ["alpha"]


def test_empty_session_list_returns_empty() -> None:
    tmux = FakeTmuxRepository(sessions={})
    result = running_envs(tmux, "mp")
    assert result == []


def test_no_sessions_match_prefix_returns_empty() -> None:
    tmux = FakeTmuxRepository(
        sessions={
            "other-alpha": {},
            "unrelated": {},
        }
    )
    result = running_envs(tmux, "mp")
    assert result == []


# ---------------------------------------------------------------------------
# Multiple envs — order preserved
# ---------------------------------------------------------------------------


def test_multiple_matching_sessions_all_returned() -> None:
    tmux = FakeTmuxRepository(
        sessions={
            "mp-alpha": {},
            "mp-beta": {},
            "mp-gamma": {},
        }
    )
    result = running_envs(tmux, "mp")
    assert result == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# Mixed matching and non-matching
# ---------------------------------------------------------------------------


def test_mixed_sessions_only_matching_stripped() -> None:
    tmux = FakeTmuxRepository(
        sessions={
            "mp-alpha": {},
            "other-alpha": {},
            "mp-beta": {},
            "mp": {},  # prefix itself (no dash+name) — should NOT match
        }
    )
    result = running_envs(tmux, "mp")
    # "mp-alpha" → "alpha", "mp-beta" → "beta"
    # "other-alpha" — no prefix match
    # "mp" — startswith("mp-") is False
    assert sorted(result) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Different prefix
# ---------------------------------------------------------------------------


def test_different_prefix() -> None:
    tmux = FakeTmuxRepository(
        sessions={
            "ws-alpha": {},
            "ws-gamma": {},
            "mp-alpha": {},
        }
    )
    result = running_envs(tmux, "ws")
    assert sorted(result) == ["alpha", "gamma"]
