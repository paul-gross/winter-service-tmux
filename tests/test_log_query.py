"""Tests for service_orchestrator.modules.orchestrate.log_query.

Covers parse_tail() and LogQuery.from_env() — the WINTER_LOG_* input adapter.
"""

from __future__ import annotations

import io

from service_orchestrator.modules.orchestrate.log_query import LogQuery, parse_tail

# ---------------------------------------------------------------------------
# parse_tail: empty string → None
# ---------------------------------------------------------------------------


def test_parse_tail_empty_string_returns_none() -> None:
    assert parse_tail("") is None


def test_parse_tail_empty_string_no_warning() -> None:
    sink = io.StringIO()
    parse_tail("", err_sink=sink)
    assert sink.getvalue() == ""


# ---------------------------------------------------------------------------
# parse_tail: "all" → None
# ---------------------------------------------------------------------------


def test_parse_tail_all_returns_none() -> None:
    assert parse_tail("all") is None


def test_parse_tail_all_no_warning() -> None:
    sink = io.StringIO()
    parse_tail("all", err_sink=sink)
    assert sink.getvalue() == ""


# ---------------------------------------------------------------------------
# parse_tail: valid integer string → int
# ---------------------------------------------------------------------------


def test_parse_tail_integer_string_returns_int() -> None:
    assert parse_tail("50") == 50


def test_parse_tail_integer_string_no_warning() -> None:
    sink = io.StringIO()
    result = parse_tail("50", err_sink=sink)
    assert result == 50
    assert sink.getvalue() == ""


# ---------------------------------------------------------------------------
# parse_tail: invalid string → None with exact warning text
# ---------------------------------------------------------------------------


def test_parse_tail_invalid_returns_none() -> None:
    sink = io.StringIO()
    result = parse_tail("notanumber", err_sink=sink)
    assert result is None


def test_parse_tail_invalid_writes_exact_warning() -> None:
    sink = io.StringIO()
    parse_tail("notanumber", err_sink=sink)
    assert (
        sink.getvalue().strip()
        == "orchestrate: WINTER_LOG_TAIL 'notanumber' is not a valid integer or 'all'; treating as 'all'"
    )


def test_parse_tail_invalid_warning_contains_raw_value() -> None:
    sink = io.StringIO()
    parse_tail("bad_val", err_sink=sink)
    assert "bad_val" in sink.getvalue()


# ---------------------------------------------------------------------------
# parse_tail: whitespace-padded values are stripped
# ---------------------------------------------------------------------------


def test_parse_tail_whitespace_around_all_returns_none() -> None:
    assert parse_tail("  all  ") is None


def test_parse_tail_whitespace_around_integer_returns_int() -> None:
    assert parse_tail("  10  ") == 10


# ---------------------------------------------------------------------------
# parse_tail: default err_sink is sys.stderr (smoke — no assertion on output)
# ---------------------------------------------------------------------------


def test_parse_tail_default_err_sink_does_not_raise() -> None:
    """Calling parse_tail with an invalid value and no err_sink must not raise."""
    result = parse_tail("bogus")
    assert result is None


# ---------------------------------------------------------------------------
# LogQuery.from_env: all vars present
# ---------------------------------------------------------------------------


def test_from_env_all_vars_present() -> None:
    env = {
        "WINTER_LOG_FOLLOW": "1",
        "WINTER_LOG_TAIL": "100",
        "WINTER_LOG_SINCE": "2026-01-01T00:00:00Z",
        "WINTER_LOG_UNTIL": "2026-12-31T00:00:00Z",
        "WINTER_LOG_TIMESTAMPS": "1",
    }
    query = LogQuery.from_env(("backend",), env)
    assert query.follow is True
    assert query.tail == 100
    assert query.since == "2026-01-01T00:00:00Z"
    assert query.until == "2026-12-31T00:00:00Z"
    assert query.timestamps is True
    assert query.services == ("backend",)


# ---------------------------------------------------------------------------
# LogQuery.from_env: defaults when vars absent
# ---------------------------------------------------------------------------


def test_from_env_defaults_when_absent() -> None:
    query = LogQuery.from_env((), {})
    assert query.follow is False
    assert query.tail is None  # default "all" → None
    assert query.since == ""
    assert query.until == ""
    assert query.timestamps is False
    assert query.services == ()


# ---------------------------------------------------------------------------
# LogQuery.from_env: TAIL="all" → None
# ---------------------------------------------------------------------------


def test_from_env_tail_all_returns_none() -> None:
    query = LogQuery.from_env((), {"WINTER_LOG_TAIL": "all"})
    assert query.tail is None


# ---------------------------------------------------------------------------
# LogQuery.from_env: TAIL absent defaults to "all" → None
# ---------------------------------------------------------------------------


def test_from_env_tail_absent_defaults_to_none() -> None:
    query = LogQuery.from_env((), {})
    assert query.tail is None


# ---------------------------------------------------------------------------
# LogQuery.from_env: FOLLOW="0" → False
# ---------------------------------------------------------------------------


def test_from_env_follow_zero_is_false() -> None:
    query = LogQuery.from_env((), {"WINTER_LOG_FOLLOW": "0"})
    assert query.follow is False


# ---------------------------------------------------------------------------
# LogQuery.from_env: TIMESTAMPS="0" → False
# ---------------------------------------------------------------------------


def test_from_env_timestamps_zero_is_false() -> None:
    query = LogQuery.from_env((), {"WINTER_LOG_TIMESTAMPS": "0"})
    assert query.timestamps is False


# ---------------------------------------------------------------------------
# LogQuery.from_env: services tuple is preserved
# ---------------------------------------------------------------------------


def test_from_env_services_tuple_preserved() -> None:
    svcs = ("backend", "worker")
    query = LogQuery.from_env(svcs, {})
    assert query.services == svcs
