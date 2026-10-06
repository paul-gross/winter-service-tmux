"""The dedicated tmux server every winter session runs on.

Sessions live on the named socket ``tmux -L winter``, never the user's default
server.  tmux seeds a server's global environment — which every pane inherits
— from whichever client first starts it, so the server is always started from
``clean_server_env`` rather than the caller's environment.  Otherwise the
first ``up`` would leak its feature env's (or its workspace's) variables into
every later session on the machine.

The server still loads the user's own ``~/.tmux.conf``, so their key bindings
and look carry over on attach.  Two settings there change what panes see: a
``default-command`` that skips login shells leaves panes on the baseline
``PATH``, and ``set-environment -g`` adds variables to the global environment
that ``leaked_names`` then reports.
"""

from __future__ import annotations

from collections.abc import Mapping

TMUX_SOCKET_NAME = "winter"

# A fixed system baseline for Linux and macOS, including Homebrew's Apple
# Silicon prefix so the server's own children (``run-shell`` lines and status
# commands from ``~/.tmux.conf``) still find tmux and its plugins; directories
# missing on a platform are harmless.  Pane shells are login shells, so the
# user's profile rebuilds their own PATH on top.
BASELINE_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Per-user (never per-workspace) variables the server needs to behave like a
# fresh login: identity, shell, terminal, temp dirs, the socket directory
# (``TMUX_TMPDIR`` must match the caller's or ``-L winter`` resolves elsewhere),
# and login-session credentials — the ssh agent for git over ssh, Kerberos
# tickets, and the display, its auth cookie, and session bus for a dev server
# opening a browser.  Of tmux's default ``update-environment`` list, only
# ``SSH_CONNECTION`` and ``WINDOWID`` stay out: they describe the one terminal
# that happened to start the server, not the user.
PASSTHROUGH = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "TMPDIR",
    "TMUX_TMPDIR",
    "XDG_RUNTIME_DIR",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SSH_ASKPASS",
    "KRB5CCNAME",
    "DISPLAY",
    "XAUTHORITY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    # macOS CoreFoundation's per-user text encoding, set on every process.
    "__CF_USER_TEXT_ENCODING",
)


def clean_server_env(source: Mapping[str, str]) -> dict[str, str]:
    """Return the allow-listed subset of *source* with ``PATH`` reset to the baseline."""
    env = {name: source[name] for name in PASSTHROUGH if name in source}
    env.update({name: value for name, value in source.items() if name.startswith("LC_")})
    env["PATH"] = BASELINE_PATH
    return env


def leaked_names(server_env: Mapping[str, str]) -> list[str]:
    """Return the names in a running server's global environment a clean start never sets.

    ``PATH`` counts as leaked unless it is still the baseline.  ``PWD`` is
    allowed: tmux records the starting client's working directory itself.
    """
    leaked = []
    for name, value in server_env.items():
        if name in PASSTHROUGH or name.startswith("LC_") or name == "PWD":
            continue
        if name == "PATH" and value == BASELINE_PATH:
            continue
        leaked.append(name)
    return sorted(leaked)
