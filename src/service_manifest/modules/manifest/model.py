from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

# Valid port expression: optional whitespace around "WINTER_PORT_BASE + <int>"
_PORT_EXPR_RE = re.compile(r"^\s*WINTER_PORT_BASE\s*\+\s*(\d+)\s*$")


def parse_port_expression(s: str) -> int | None:
    """Parse a ``WINTER_PORT_BASE + <offset>`` expression and return the offset.

    Returns the integer offset when *s* matches the expression, or ``None``
    when it does not match.  The caller adds the offset to the resolved
    ``WINTER_PORT_BASE`` value to obtain the absolute port number.
    """
    m = _PORT_EXPR_RE.match(s)
    return int(m.group(1)) if m else None


class LogMode(StrEnum):
    """Log capture mode for a declared service.

    Values:
        FILE:   Capture output to ``<env>/.winter/logs/<svc>.log`` via the
                capture writer (default).  Timestamps are present; logs persist
                across restarts and ``down``.
        PANE:   Do NOT wrap the launch line; read the pane buffer on demand via
                ``tmux capture-pane``.  No timestamps; no persistence; requires
                a running session.  Natural fit for interactive panes (``shell``
                service) or services where TTY preservation matters more than
                persistence.
        MEMORY: Accept the value for forward-compatibility; not yet implemented.
                ``logs`` emits nothing for services in this mode (future work).
    """

    FILE = "file"
    PANE = "pane"
    MEMORY = "memory"


class HealthType(StrEnum):
    """Supported service readiness probe types."""

    URL = "url"
    CMD = "cmd"


@dataclass(frozen=True)
class Health:
    """Optional readiness probe for a declared service.

    ``target`` may contain ``${VAR}`` placeholders resolved against the env file
    before the probe runs.  ``timeout`` is seconds; ``None`` means use the probe
    runner's default timeout.
    """

    type: HealthType
    target: str
    timeout: float | None = None


@dataclass(frozen=True)
class StartupPolicy:
    """Optional startup retry policy for a declared service.

    ``retries`` is the max re-launch attempts after the first failure (0 = no
    retry).  ``retry_delay`` is seconds to wait between attempts; the default
    applies when the field is omitted from TOML.  Honored by
    ``winter service up``; the env-root ``./up`` does not retry.
    """

    retries: int = 0
    retry_delay: float = 2.0


@dataclass(frozen=True)
class LogConfig:
    """Log-capture configuration for the manifest.

    All fields are optional in the TOML ``[logs]`` table; these are the defaults
    applied when a key is absent.

    Fields:
        rotate_size_bytes: Rotate the active ``.log`` file once it exceeds this
            size in bytes.  Default 10 MiB.
        max_rotations: Maximum number of rotated segments to keep on disk
            (``.log.1`` … ``.log.<n>``).  Zero disables rotation (unbounded
            growth). Default 5.
        retention_seconds: Delete rotated segments older than this many seconds
            during a prune pass.  Zero disables time-based pruning. Default 7
            days (604800 s).
    """

    rotate_size_bytes: int = 10485760  # 10 MiB
    max_rotations: int = 5
    retention_seconds: int = 604800  # 7 days


@dataclass(frozen=True)
class Target:
    """A tmux pane address: window index and pane index within that window.

    Both are zero-based integers.  The TOML representation is a dotted string
    like ``"0.0"`` (parsed by the reader); this dataclass holds the decoded
    integers.
    """

    window: int
    pane: int


@dataclass(frozen=True)
class Service:
    """A single declared service in the manifest.

    ``cmd`` may be an empty string — that signals an interactive pane:
    the pane gets the env sourced and a banner, then sits at a prompt (matching
    the bash ``winter_service_cmd shell ""`` convention).

    Fields:
        name: Unique identifier for the service.
        target: Tmux pane address.
        cmd: Shell command to run; empty string means interactive pane.
        log: Log capture mode.  Default ``LogMode.FILE``.  Empty-cmd
            (interactive) services are always launched bare regardless of this
            field.  ``LogMode.FILE`` captures output to a persisted file via the
            capture writer.  ``LogMode.PANE`` reads the pane buffer via
            ``tmux capture-pane`` (no file persistence, no timestamps, requires
            a running session).  ``LogMode.MEMORY`` is accepted but not yet
            implemented (stub).
        health: Optional readiness probe. Services without a probe report
            ``health = "unknown"`` in winter's status document.
        startup: Optional startup retry policy. ``None`` means no retry;
            ``winter service up`` honors it, the env-root ``./up`` does not.
        port: Optional declared port for this service.  Either a literal integer
            or a ``WINTER_PORT_BASE + <offset>`` expression (e.g.
            ``"WINTER_PORT_BASE + 10"``).  Stored as-is (the raw parsed value);
            the orchestrator resolves the expression against the env's
            ``WINTER_PORT_BASE`` at status time.  ``None`` when not declared —
            the service renders blank in the ``PORTS`` column.

            The bespoke ``WINTER_PORT_BASE + <int>`` form is intentionally
            distinct from the manifest's ``${VAR}`` interpolation used by
            ``health.target`` and ``status_url``.  Offset arithmetic — adding a
            literal integer to an env-supplied base — is not expressible via
            ``${...}`` substitution (which only performs verbatim string
            replacement), so a small dedicated syntax is used here.  The
            divergence is a conscious design choice, not an inconsistency.
    """

    name: str
    target: Target
    cmd: str
    log: LogMode = LogMode.FILE
    health: Health | None = None
    startup: StartupPolicy | None = None
    port: int | str | None = None


@dataclass(frozen=True)
class ServiceManifest:
    """The fully-parsed, immutable manifest for a feature environment.

    Fields:
        session_prefix: Optional explicit override for the tmux session-name
            prefix; sessions are named ``<prefix>-<env>``. When declared, this
            value always wins over winter's injected ``WINTER_SERVICE_PREFIX``
            environment variable (see ``SessionContextBuilder``). ``None``
            (the default — omit the key entirely) means the prefix is
            resolved solely from ``WINTER_SERVICE_PREFIX`` at dispatch time.
        env_file: Path to the env file, relative to the worktree root.
            ``None`` when not declared (env sourcing is skipped).
        layout_hook: Path to the optional bash layout hook, relative to the
            workspace root.  ``None`` when not declared.
        services: All declared services, in declaration order.
        logs: Log-capture configuration.  Always present (defaulted); consumers
            need no null-guard.
        workspace_services: Services scoped to the shared ``<prefix>-workspace``
            singleton session, in declaration order.  Empty when none declared.
        workspace_layout_hook: Optional bash layout hook for the workspace
            session, relative to the workspace root.  ``None`` when not declared.
    """

    session_prefix: str | None
    env_file: str | None
    layout_hook: str | None
    services: tuple[Service, ...]
    logs: LogConfig = field(default_factory=LogConfig)
    workspace_services: tuple[Service, ...] = ()
    workspace_layout_hook: str | None = None
