"""Pure formatting helpers for the orchestrator's status output.

All functions here take only plain values (stdlib types, domain dataclasses)
and return values with no I/O — they are free functions per the service-
architecture rule.

``build_launch_line`` assembles the tmux send-keys launch line for a service
(env-source, banner, command — same logic as the former bash
``winter_tmux_send_service`` helper).  When *logfile* and *capture_params* are
supplied the command is wrapped to pipe through the capture writer so output is
persisted to the log file while the pane stays live.
``last_non_blank_line`` and ``truncate_status_line`` format per-pane output
for the status display.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def logwriter_path() -> Path:
    """Return the absolute path to the standalone ``logwriter.py`` script.

    Resolution order:
    1. If ``WINTER_EXT_DIR`` is set (winter sets it on orchestrator dispatch
       via the ``winter service`` entrypoint), return
       ``$WINTER_EXT_DIR/src/service_orchestrator/logwriter.py``.
    2. Otherwise fall back to the ``__file__``-relative path (used when
       invoked via the env-root ``./up`` symlink, which does not set
       ``WINTER_EXT_DIR``).  ``logwriter.py`` lives at
       ``src/service_orchestrator/logwriter.py``; this file is at
       ``src/service_orchestrator/modules/orchestrate/``, so we walk up two
       package levels to reach the ``service_orchestrator`` root.
    """
    ext_dir = os.environ.get("WINTER_EXT_DIR")
    if ext_dir:
        return Path(ext_dir) / "src" / "service_orchestrator" / "logwriter.py"
    return Path(__file__).resolve().parents[2] / "logwriter.py"


def build_launch_line(
    worktree_dir: Path,
    scope: str | None,
    name: str,
    command: str,
    env_file_path: Path | None = None,
    logfile: Path | None = None,
    rotate_size_bytes: int | None = None,
    max_rotations: int | None = None,
    cwd: str | None = None,
) -> str:
    """Build the tmux send-keys launch line for one service.

    Assembles the pane launch line::

        cd '<worktree_dir>' [&& eval "$(winter env '<scope>')"] [&& . '<env_file>']
            && echo '=== <name> ===' [&& <command>]

    When *cwd* is not ``None`` (the manifest service's optional ``cwd`` field,
    already normalized by the reader) the leading ``cd`` targets
    ``<worktree_dir>/<cwd>`` instead of ``<worktree_dir>`` — everything else
    (the scope self-source, env-file dot-source, banner, and capture pipe) is
    unchanged.

    When *scope* is not ``None`` the pane shell self-sources the full scope
    environment via POSIX ``eval "$(winter env <scope>)"`` before the banner.
    This brings all ``WINTER_*`` base vars and the scope's env-var band entries
    (``[env.workspace.vars]`` / ``[env.feature.vars]``) into the
    pane without the orchestrator process executing ``winter`` itself — the
    entry shim already sourced the scope env before ``exec python3``, and each
    pane self-sources independently.

    When *env_file_path* is not ``None`` a POSIX dot-source (``&&
    . '<env_file_path>'``) is appended after the ``eval "$(winter env ...)"``
    segment (or after ``cd <wt>`` when *scope* is ``None``).  This layers
    machine-specific credentials from the manifest ``env_file`` (e.g.
    ``.env.local``) on top of winter's scope vars.

    When *scope* is ``None`` (local/env-less mode or the workspace session) no
    eval prefix is added — just ``cd <wt>``.

    When *command* is empty (an interactive ``shell`` pane), the trailing
    ``&& <command>`` is omitted — the pane gets the banner and sits at a
    prompt, matching the bash convention.

    When *logfile*, *rotate_size_bytes*, and *max_rotations* are all supplied
    **and** *command* is non-empty, the command is wrapped so its stdout and
    stderr pipe through the capture writer::

        cd '<wt>' [&& eval "$(winter env '<scope>')"] [&& . '<env_file>'] && echo '=== <name> ===' &&
        { <command> ; } 2>&1 | '<sys.executable>' '<writer>' '<logfile>'
        --rotate-size <N> --max-rotations <M>

    The brace group ``{ <command> ; }`` ensures the redirect and pipe apply to
    the entire command even when it contains ``&&`` or inner pipes.  The writer
    echoes every raw line to stdout so the pane stays live.  The pipe invokes
    the orchestrator's own interpreter (``sys.executable``, shell-quoted)
    rather than a bare ``python3`` resolved fresh from the pane's ``PATH`` —
    this guarantees the writer runs under the same 3.11+ interpreter the
    orchestrator itself runs under, even when the host's ``python3`` resolves
    to an older interpreter.
    """
    target_dir = worktree_dir / cwd if cwd else worktree_dir
    prefix = f"cd {shlex.quote(str(target_dir))}"
    if scope is not None:
        prefix = f'{prefix} && eval "$(winter env {shlex.quote(scope)})"'
    if env_file_path is not None:
        prefix = f"{prefix} && . {shlex.quote(str(env_file_path))}"

    quoted_banner = shlex.quote(f"=== {name} ===")
    line = f"{prefix} && echo {quoted_banner}"
    if command:
        capture = logfile is not None and rotate_size_bytes is not None and max_rotations is not None
        if capture:
            writer = logwriter_path()
            line = (
                f"{line} && "
                f"{{ {command} ; }} 2>&1 | "
                f"{shlex.quote(sys.executable)} {shlex.quote(str(writer))} {shlex.quote(str(logfile))} "
                f"--rotate-size {rotate_size_bytes} "
                f"--max-rotations {max_rotations}"
            )
        else:
            line = f"{line} && {command}"
    return line


def build_service_status(
    name: str,
    state: str,
    *,
    health: str = "unknown",
    handle: str | None,
    log_path: str | None,
    ports: list[int] | None = None,
) -> dict:  # type: ignore[type-arg]
    """Build one service entry for winter's env-keyed status document.

    Shape-stable per the winter ``status`` wire contract: every field is always
    present.  ``health`` is ``"healthy"``/``"unhealthy"`` when a service declares
    a readiness probe and ``"unknown"`` otherwise.  ``since`` is unpopulated
    (``None``) because tmux does not track it.  ``ports`` carries the declared
    port(s) for services that declare a ``port`` field in the manifest; it is
    ``[]`` when no port is declared.

    Args:
        name: Service name.
        state: One of ``"running"`` | ``"stopped"`` | ``"unknown"``.
        handle: The tmux pane address (``<session>:<window>.<pane>``) or
            ``None`` when no live pane backs the service.
        log_path: Absolute path to the captured log file, or ``None`` when the
            service is not file-logged.
        ports: List of declared port numbers for this service, or ``None``/``[]``
            when no port is declared.
    """
    return {
        "name": name,
        "state": state,
        "health": health,
        "ports": ports if ports else [],
        "handle": handle,
        "log_path": log_path,
        "since": None,
    }


def build_env_status(
    env: str,
    session: str | None,
    port_base: int | None,
    services: list[dict],  # type: ignore[type-arg]
) -> dict:  # type: ignore[type-arg]
    """Build one env's entry in winter's env-keyed status document.

    ``session``/``port_base`` are ``None`` when undeterminable, per the
    shape-stability rule.
    """
    return {
        "env": env,
        "session": session,
        "port_base": port_base,
        "services": services,
    }


def build_status_document(env_docs: list[dict]) -> dict:  # type: ignore[type-arg]
    """Build the top-level env-keyed status document: ``{"envs": [...]}``.

    An empty *env_docs* yields ``{"envs": []}``, a valid non-error document
    (no services currently visible).
    """
    return {"envs": env_docs}


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
