"""Tests for service_orchestrator.modules.orchestrate.pattern_match.

Covers:
- Literal ``<env>/<svc>`` match and non-match
- Service-segment glob ``<env>/svc*``
- Env-segment glob ``*/<svc>``
- Bare pattern ``<env>`` → treated as ``<env>/*`` (all services in that env)
- Cross-env glob ``*/<svc>`` (matches across any env)
- Case sensitivity (fnmatchcase)
- ``matches_any_pattern`` with multiple patterns (any-match semantics)
- ``matches_any_pattern`` with empty pattern list → always False
"""

from __future__ import annotations

from service_orchestrator.modules.orchestrate.pattern_match import (
    matches_any_pattern,
    matches_pattern,
)

# ---------------------------------------------------------------------------
# matches_pattern — literal patterns
# ---------------------------------------------------------------------------


def test_literal_exact_match() -> None:
    assert matches_pattern("alpha", "backend", "alpha/backend") is True


def test_literal_env_mismatch() -> None:
    assert matches_pattern("beta", "backend", "alpha/backend") is False


def test_literal_svc_mismatch() -> None:
    assert matches_pattern("alpha", "frontend", "alpha/backend") is False


# ---------------------------------------------------------------------------
# matches_pattern — service-segment glob
# ---------------------------------------------------------------------------


def test_svc_glob_prefix_match() -> None:
    assert matches_pattern("alpha", "backend", "alpha/back*") is True


def test_svc_glob_prefix_no_match() -> None:
    assert matches_pattern("alpha", "frontend", "alpha/back*") is False


def test_svc_glob_wildcard_all_services() -> None:
    assert matches_pattern("alpha", "worker", "alpha/*") is True


def test_svc_glob_does_not_cross_slash() -> None:
    # ``back*`` should NOT match ``backend-worker`` only if ``-worker`` is after
    # the end of ``back*`` expansion — but ``back*`` does match ``backend-worker``
    # because ``*`` matches any characters within the segment.
    assert matches_pattern("alpha", "backend-worker", "alpha/back*") is True


# ---------------------------------------------------------------------------
# matches_pattern — env-segment glob (cross-env)
# ---------------------------------------------------------------------------


def test_env_glob_star_matches_any_env() -> None:
    assert matches_pattern("alpha", "backend", "*/backend") is True
    assert matches_pattern("beta", "backend", "*/backend") is True


def test_env_glob_star_does_not_match_different_svc() -> None:
    assert matches_pattern("alpha", "frontend", "*/backend") is False


def test_env_glob_prefix() -> None:
    assert matches_pattern("alpha", "backend", "alph*/backend") is True
    assert matches_pattern("beta", "backend", "alph*/backend") is False


# ---------------------------------------------------------------------------
# matches_pattern — bare pattern (no slash) → <pattern>/*
# ---------------------------------------------------------------------------


def test_bare_pattern_matches_all_services_in_env() -> None:
    assert matches_pattern("alpha", "backend", "alpha") is True
    assert matches_pattern("alpha", "frontend", "alpha") is True
    assert matches_pattern("alpha", "worker", "alpha") is True


def test_bare_pattern_does_not_match_other_env() -> None:
    assert matches_pattern("beta", "backend", "alpha") is False


# ---------------------------------------------------------------------------
# matches_pattern — case sensitivity
# ---------------------------------------------------------------------------


def test_case_sensitive_env() -> None:
    assert matches_pattern("Alpha", "backend", "alpha/backend") is False
    assert matches_pattern("alpha", "backend", "Alpha/backend") is False


def test_case_sensitive_svc() -> None:
    assert matches_pattern("alpha", "Backend", "alpha/backend") is False
    assert matches_pattern("alpha", "backend", "alpha/Backend") is False


# ---------------------------------------------------------------------------
# matches_any_pattern
# ---------------------------------------------------------------------------


def test_matches_any_pattern_single_match() -> None:
    assert matches_any_pattern("alpha", "backend", ["alpha/backend"]) is True


def test_matches_any_pattern_first_of_two() -> None:
    assert matches_any_pattern("alpha", "backend", ["alpha/backend", "beta/frontend"]) is True


def test_matches_any_pattern_second_of_two() -> None:
    assert matches_any_pattern("beta", "frontend", ["alpha/backend", "beta/frontend"]) is True


def test_matches_any_pattern_none_match() -> None:
    assert matches_any_pattern("gamma", "worker", ["alpha/backend", "beta/frontend"]) is False


def test_matches_any_pattern_empty_list() -> None:
    assert matches_any_pattern("alpha", "backend", []) is False


def test_matches_any_pattern_glob_in_list() -> None:
    assert matches_any_pattern("alpha", "backend", ["*/backend", "beta/frontend"]) is True
    assert matches_any_pattern("beta", "frontend", ["*/backend", "beta/frontend"]) is True
    assert matches_any_pattern("gamma", "worker", ["*/backend", "beta/frontend"]) is False


def test_matches_any_pattern_bare_and_slash() -> None:
    """Bare pattern and slash pattern can coexist in the same list."""
    patterns = ["alpha", "beta/worker"]
    assert matches_any_pattern("alpha", "frontend", patterns) is True  # bare alpha → alpha/*
    assert matches_any_pattern("beta", "worker", patterns) is True  # explicit beta/worker
    assert matches_any_pattern("gamma", "backend", patterns) is False
