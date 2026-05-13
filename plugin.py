"""Dashboard plugin: paint a ●/○ tmux-session badge on each feature env header.

Discovered automatically by `winter`'s plugin loader because this file lives at
the root of an installed extension. The decorator runs once per environment per
dashboard refresh and writes a one-character badge into
`env_status.extensions["wst"]`, which the matrix grid and detail header append
to the env's name.

Hooks (e.g. `on_env_init`) stay in `winter-ext.toml` for agent-facing CLI
integration; this plugin file is the visual side of the same extension.
"""

from __future__ import annotations

import dataclasses
import subprocess


def create_plugin():
    return TmuxStatusPlugin()


@dataclasses.dataclass
class TmuxStatusPlugin:
    name: str = "winter-service-tmux"

    def register(self, config):
        # Lazy import keeps the plugin loadable when winter-cli isn't in sys.path
        # at module-import time (e.g. when type-checked standalone).
        from winter_cli.plugins.types import PluginRegistration
        return PluginRegistration(environment_decorators=[tmux_session_badge])


def tmux_session_badge(env_status, env_path) -> None:
    """Probe `tmux has-session -t <prefix>-<env>` and stamp a badge on env_status.

    Filled circle = session running, hollow circle = stopped. tmux missing or
    timing out is treated as stopped — never raises, since failures here would
    blank out the whole dashboard column.
    """
    session = f"{env_status.environment.workspace.session_prefix}-{env_status.environment.name}"
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            timeout=2,
        )
        running = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        running = False
    env_status.extensions["wst"] = "●" if running else "○"
