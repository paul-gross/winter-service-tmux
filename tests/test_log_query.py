"""Tests for service_orchestrator.modules.orchestrate.log_query.

Covers parse_tail(), parse_log_args() — the argv render-option parser — and
LogQuery.from_render().
"""

from __future__ import annotations

import io

from service_orchestrator.modules.orchestrate.log_query import (
    LogQuery,
    LogRenderOptions,
    parse_log_args,
    parse_tail,
)

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
        == "orchestrate: --tail 'notanumber' is not a valid integer or 'all'; treating as 'all'"
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
# parse_log_args: positional patterns only, flags absent → defaults
# ---------------------------------------------------------------------------


def test_parse_log_args_patterns_only_defaults() -> None:
    patterns, render = parse_log_args(["alpha/backend", "alpha/worker"])
    assert patterns == ["alpha/backend", "alpha/worker"]
    assert render.follow is False
    assert render.tail is None  # default "all" → None
    assert render.since == ""
    assert render.until == ""
    assert render.timestamps is False


# ---------------------------------------------------------------------------
# parse_log_args: each flag is parsed off argv
# ---------------------------------------------------------------------------


def test_parse_log_args_follow_long_and_short() -> None:
    _, render = parse_log_args(["alpha/backend", "--follow"])
    assert render.follow is True
    _, render = parse_log_args(["alpha/backend", "-f"])
    assert render.follow is True


def test_parse_log_args_timestamps_long_and_short() -> None:
    _, render = parse_log_args(["alpha/backend", "--timestamps"])
    assert render.timestamps is True
    _, render = parse_log_args(["alpha/backend", "-t"])
    assert render.timestamps is True


def test_parse_log_args_tail_long_and_short() -> None:
    _, render = parse_log_args(["alpha/backend", "--tail", "50"])
    assert render.tail == 50
    _, render = parse_log_args(["alpha/backend", "-n", "100"])
    assert render.tail == 100


def test_parse_log_args_tail_all() -> None:
    _, render = parse_log_args(["alpha/backend", "--tail", "all"])
    assert render.tail is None


def test_parse_log_args_since_until_consumed_as_is() -> None:
    patterns, render = parse_log_args(
        [
            "alpha/backend",
            "--since",
            "2026-01-01T00:00:00Z",
            "--until",
            "2026-12-31T00:00:00Z",
        ]
    )
    assert patterns == ["alpha/backend"]
    assert render.since == "2026-01-01T00:00:00Z"
    assert render.until == "2026-12-31T00:00:00Z"


def test_parse_log_args_all_flags_together() -> None:
    patterns, render = parse_log_args(
        [
            "alpha/backend",
            "-f",
            "-n",
            "25",
            "--since",
            "2026-01-01T00:00:00Z",
            "--until",
            "2026-12-31T00:00:00Z",
            "-t",
        ]
    )
    assert patterns == ["alpha/backend"]
    assert render.follow is True
    assert render.tail == 25
    assert render.since == "2026-01-01T00:00:00Z"
    assert render.until == "2026-12-31T00:00:00Z"
    assert render.timestamps is True


# ---------------------------------------------------------------------------
# LogQuery.from_render: folds per-env services into the render options
# ---------------------------------------------------------------------------


def test_from_render_combines_services_and_options() -> None:
    render = LogRenderOptions(
        follow=True,
        tail=100,
        since="2026-01-01T00:00:00Z",
        until="2026-12-31T00:00:00Z",
        timestamps=True,
    )
    query = LogQuery.from_render(("backend",), render)
    assert query.follow is True
    assert query.tail == 100
    assert query.since == "2026-01-01T00:00:00Z"
    assert query.until == "2026-12-31T00:00:00Z"
    assert query.timestamps is True
    assert query.services == ("backend",)


def test_from_render_services_tuple_preserved() -> None:
    svcs = ("backend", "worker")
    render = LogRenderOptions(follow=False, tail=None, since="", until="", timestamps=False)
    query = LogQuery.from_render(svcs, render)
    assert query.services == svcs
