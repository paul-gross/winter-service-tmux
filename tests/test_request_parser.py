"""Tests for service_orchestrator.modules.orchestrate.request_parser.

Covers all parse_request() paths: empty argv, unknown action, up/down arity,
restart/logs zero-pattern, valid requests for every action, and the
dash-leading pattern tolerance guarantee.
"""

from __future__ import annotations

import pytest

from service_orchestrator.modules.orchestrate.request_parser import (
    _ACTIONS,
    OrchestratorRequest,
    ParseError,
    parse_request,
)

# ---------------------------------------------------------------------------
# Empty argv → usage, rc 2
# ---------------------------------------------------------------------------


def test_empty_argv_returns_parse_error_rc2() -> None:
    result = parse_request([])
    assert isinstance(result, ParseError)
    assert result.exit_code == 2
    assert "usage" in result.message.lower()
    assert "action" in result.message.lower()


def test_empty_argv_message_lists_actions() -> None:
    result = parse_request([])
    assert isinstance(result, ParseError)
    for action in _ACTIONS:
        assert action in result.message


# ---------------------------------------------------------------------------
# Unknown action → rc 2
# ---------------------------------------------------------------------------


def test_unknown_action_returns_parse_error_rc2() -> None:
    result = parse_request(["badaction"])
    assert isinstance(result, ParseError)
    assert result.exit_code == 2


def test_unknown_action_message_contains_action_name() -> None:
    result = parse_request(["badaction", "alpha"])
    assert isinstance(result, ParseError)
    assert "badaction" in result.message


def test_unknown_action_message_contains_expected_actions() -> None:
    result = parse_request(["fly"])
    assert isinstance(result, ParseError)
    for action in _ACTIONS:
        assert action in result.message


# ---------------------------------------------------------------------------
# up / down: arity checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["up", "down"])
def test_up_down_zero_positionals_returns_parse_error_rc2(action: str) -> None:
    result = parse_request([action])
    assert isinstance(result, ParseError)
    assert result.exit_code == 2


@pytest.mark.parametrize("action", ["up", "down"])
def test_up_down_two_positionals_returns_parse_error_rc2(action: str) -> None:
    result = parse_request([action, "alpha", "extra"])
    assert isinstance(result, ParseError)
    assert result.exit_code == 2


@pytest.mark.parametrize("action", ["up", "down"])
def test_up_down_usage_message_contains_action(action: str) -> None:
    result = parse_request([action])
    assert isinstance(result, ParseError)
    assert action in result.message


# ---------------------------------------------------------------------------
# up / down: valid request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["up", "down"])
def test_up_down_valid_returns_request(action: str) -> None:
    result = parse_request([action, "alpha"])
    assert isinstance(result, OrchestratorRequest)
    assert result.action == action
    assert result.env == "alpha"
    assert result.patterns == []


# ---------------------------------------------------------------------------
# restart: zero patterns → rc 1
# ---------------------------------------------------------------------------


def test_restart_zero_patterns_returns_parse_error_rc1() -> None:
    result = parse_request(["restart"])
    assert isinstance(result, ParseError)
    assert result.exit_code == 1


def test_restart_zero_patterns_message_mentions_restart() -> None:
    result = parse_request(["restart"])
    assert isinstance(result, ParseError)
    assert "restart" in result.message.lower()


# ---------------------------------------------------------------------------
# restart: valid request
# ---------------------------------------------------------------------------


def test_restart_single_pattern_returns_request() -> None:
    result = parse_request(["restart", "alpha/backend"])
    assert isinstance(result, OrchestratorRequest)
    assert result.action == "restart"
    assert result.env is None
    assert result.patterns == ["alpha/backend"]


def test_restart_multiple_patterns_returns_request() -> None:
    result = parse_request(["restart", "alpha/backend", "alpha/worker"])
    assert isinstance(result, OrchestratorRequest)
    assert result.patterns == ["alpha/backend", "alpha/worker"]


# ---------------------------------------------------------------------------
# logs: zero patterns → rc 1
# ---------------------------------------------------------------------------


def test_logs_zero_patterns_returns_parse_error_rc1() -> None:
    result = parse_request(["logs"])
    assert isinstance(result, ParseError)
    assert result.exit_code == 1


def test_logs_zero_patterns_message_mentions_logs() -> None:
    result = parse_request(["logs"])
    assert isinstance(result, ParseError)
    assert "logs" in result.message.lower()


# ---------------------------------------------------------------------------
# logs: valid request
# ---------------------------------------------------------------------------


def test_logs_single_pattern_returns_request() -> None:
    result = parse_request(["logs", "alpha/backend"])
    assert isinstance(result, OrchestratorRequest)
    assert result.action == "logs"
    assert result.env is None
    assert result.patterns == ["alpha/backend"]


def test_logs_multiple_patterns_returns_request() -> None:
    result = parse_request(["logs", "*/backend", "*/worker"])
    assert isinstance(result, OrchestratorRequest)
    assert result.patterns == ["*/backend", "*/worker"]


# ---------------------------------------------------------------------------
# status: zero or more patterns
# ---------------------------------------------------------------------------


def test_status_zero_patterns_returns_request() -> None:
    result = parse_request(["status"])
    assert isinstance(result, OrchestratorRequest)
    assert result.action == "status"
    assert result.env is None
    assert result.patterns == []


def test_status_single_pattern_returns_request() -> None:
    result = parse_request(["status", "alpha/backend"])
    assert isinstance(result, OrchestratorRequest)
    assert result.patterns == ["alpha/backend"]


def test_status_multiple_patterns_returns_request() -> None:
    result = parse_request(["status", "alpha/backend", "beta/worker", "*/frontend"])
    assert isinstance(result, OrchestratorRequest)
    assert result.patterns == ["alpha/backend", "beta/worker", "*/frontend"]


# ---------------------------------------------------------------------------
# Dash-leading patterns are literal — never treated as flags
# ---------------------------------------------------------------------------


def test_dash_leading_pattern_tolerated_in_status() -> None:
    """A dash-leading token must NOT raise SystemExit or be treated as a flag."""
    try:
        result = parse_request(["status", "-alpha/backend"])
    except SystemExit:
        pytest.fail("dash-leading pattern raised SystemExit — flag parsing is happening")
    assert isinstance(result, OrchestratorRequest)
    assert result.patterns == ["-alpha/backend"]


def test_dash_leading_pattern_tolerated_in_restart() -> None:
    result = parse_request(["restart", "-alpha/backend"])
    assert isinstance(result, OrchestratorRequest)
    assert result.patterns == ["-alpha/backend"]


def test_dash_leading_pattern_tolerated_in_logs() -> None:
    result = parse_request(["logs", "-alpha/backend"])
    assert isinstance(result, OrchestratorRequest)
    assert result.patterns == ["-alpha/backend"]
