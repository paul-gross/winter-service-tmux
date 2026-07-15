"""`tmux_session_badge` reads the current `service_prefix` field and never raises.

Regression for #34: the decorator read `Workspace.session_prefix`, a field
renamed to `service_prefix` in winter-cli, so every dashboard refresh raised
`AttributeError` out of the decorator (defeating its "never raises" guard).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# plugin.py lives at the repo root, next to this package's `tests/` dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugin


def _env_status(service_prefix: str, env_name: str) -> SimpleNamespace:
    # A workspace exposing ONLY `service_prefix` — reading `session_prefix`
    # (the pre-fix field) would raise AttributeError, reproducing the bug.
    workspace = SimpleNamespace(service_prefix=service_prefix)
    environment = SimpleNamespace(workspace=workspace, name=env_name)
    return SimpleNamespace(environment=environment, extensions={})


def test_badge_resolves_service_prefixed_session_name(monkeypatch):
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0)  # session exists → running

    monkeypatch.setattr(subprocess, "run", fake_run)
    env_status = _env_status("mp", "alpha")

    plugin.tmux_session_badge(env_status, Path("/ws/alpha"))

    # Session name is `<service_prefix>-<env>`, and the running badge is stamped.
    assert captured["args"] == ["tmux", "has-session", "-t", "mp-alpha"]
    assert env_status.extensions["wst"] == "●"


def test_badge_hollow_when_session_absent(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: SimpleNamespace(returncode=1))
    env_status = _env_status("mp", "beta")

    plugin.tmux_session_badge(env_status, Path("/ws/beta"))

    assert env_status.extensions["wst"] == "○"


def test_badge_does_not_raise_and_reads_service_prefix():
    # No AttributeError escapes even without patching subprocess (tmux may be
    # absent → treated as stopped). This is the crux of #34.
    env_status = _env_status("mp", "gamma")

    plugin.tmux_session_badge(env_status, Path("/ws/gamma"))

    assert env_status.extensions["wst"] in ("●", "○")
