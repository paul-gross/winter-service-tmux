"""Tests for the sanitized environment the ``winter`` tmux server starts from."""

from __future__ import annotations

from service_orchestrator.modules.orchestrate.tmux_server import BASELINE_PATH, clean_server_env, leaked_names


def test_keeps_only_per_user_variables() -> None:
    source = {
        "HOME": "/home/u",
        "USER": "u",
        "LOGNAME": "u",
        "SHELL": "/bin/zsh",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "en_US.UTF-8",
        "TMPDIR": "/var/folders/xy/T/",
        "TMUX_TMPDIR": "/run/user/1000",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "WINTER_ENV": "alpha",
        "WINTER_SERVICE_PREFIX": "wws",
        "VIRTUAL_ENV": "/ws/alpha/.venv",
        "TMUX": "/tmp/tmux-1000/default,1,0",
        "TMUX_PANE": "%3",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "DISPLAY": ":0",
        "XAUTHORITY": "/tmp/xauth_abc",
        "SSH_CONNECTION": "10.0.0.1 5000 10.0.0.2 22",
        "WINDOWID": "4194311",
    }

    assert clean_server_env(source) == {
        "HOME": "/home/u",
        "USER": "u",
        "LOGNAME": "u",
        "SHELL": "/bin/zsh",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "en_US.UTF-8",
        "TMPDIR": "/var/folders/xy/T/",
        "TMUX_TMPDIR": "/run/user/1000",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "DISPLAY": ":0",
        "XAUTHORITY": "/tmp/xauth_abc",
        "PATH": BASELINE_PATH,
    }


def test_path_is_the_baseline_not_the_callers() -> None:
    assert clean_server_env({"PATH": "/ws/alpha/.venv/bin:/usr/bin"}) == {"PATH": BASELINE_PATH}


def test_absent_variables_stay_absent() -> None:
    assert clean_server_env({}) == {"PATH": BASELINE_PATH}


def test_a_clean_start_leaks_nothing() -> None:
    clean = clean_server_env({"HOME": "/home/u", "LC_ALL": "C", "WINTER_ENV": "alpha"})
    assert leaked_names({**clean, "PWD": "/ws/alpha"}) == []


def test_leaked_names_flags_foreign_variables_and_a_non_baseline_path() -> None:
    server_env = {"HOME": "/home/u", "PATH": "/ws/alpha/.venv/bin:/usr/bin", "WINTER_ENV": "alpha", "VIRTUAL_ENV": "x"}
    assert leaked_names(server_env) == ["PATH", "VIRTUAL_ENV", "WINTER_ENV"]
