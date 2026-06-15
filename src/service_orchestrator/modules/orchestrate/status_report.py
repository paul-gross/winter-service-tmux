"""Pure formatting helpers for the orchestrator's status output.

All functions here take only plain values (stdlib types, domain dataclasses)
and return values with no I/O — they are free functions per the service-
architecture rule.

``build_launch_line`` assembles the tmux send-keys launch line for a service
(env-source, banner, command — same logic as the former bash
``winter_tmux_send_service`` helper).
``last_non_blank_line`` and ``truncate_status_line`` format per-pane output
for the status display.
"""

from __future__ import annotations

from pathlib import Path


def build_launch_line(
    worktree_dir: Path,
    env_file_path: Path | None,
    name: str,
    command: str,
) -> str:
    """Build the tmux send-keys launch line for one service.

    Reproduces the bash ``winter_tmux_send_service`` assembly exactly::

        cd '<worktree_dir>' && [source '<env_file_path>' &&] echo '=== <name> ===' [&& <command>]

    When *command* is empty (an interactive ``shell`` pane), the trailing
    ``&& <command>`` is omitted — the pane gets the banner and sits at a
    prompt, matching the bash convention.
    """
    prefix = f"cd '{worktree_dir}'"
    if env_file_path is not None:
        prefix = f"{prefix} && source '{env_file_path}'"

    line = f"{prefix} && echo '=== {name} ==='"
    if command:
        line = f"{line} && {command}"
    return line


def last_non_blank_line(text: str) -> str:
    """Return the last non-blank line from *text*, or an empty string.

    Mirrors ``grep -v '^$' | tail -1`` from ``workflow/status``.
    """
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return ""


def truncate_status_line(line: str, width: int = 80) -> str:
    """Truncate *line* to at most *width* characters.

    Mirrors ``${LAST_LINE:0:80}`` in ``workflow/status``.
    """
    return line[:width]
