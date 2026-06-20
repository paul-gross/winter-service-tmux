from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


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

    ``command`` may be an empty string — that signals an interactive pane:
    the pane gets the env sourced and a banner, then sits at a prompt (matching
    the bash ``winter_service_cmd shell ""`` convention).

    Fields:
        name: Unique identifier for the service.
        target: Tmux pane address.
        command: Shell command to run; empty string means interactive pane.
        log: Log capture mode.  Default ``LogMode.FILE``.  Empty-command
            (interactive) services are always launched bare regardless of this
            field.  ``LogMode.FILE`` captures output to a persisted file via the
            capture writer.  ``LogMode.PANE`` reads the pane buffer via
            ``tmux capture-pane`` (no file persistence, no timestamps, requires
            a running session).  ``LogMode.MEMORY`` is accepted but not yet
            implemented (stub).
    """

    name: str
    target: Target
    command: str
    log: LogMode = LogMode.FILE


@dataclass(frozen=True)
class StatusUrl:
    """A URL entry displayed in the status header.

    ``url`` is the raw template string and may contain ``${VAR}`` placeholders
    that are resolved against the env file at validation time.
    """

    label: str
    url: str


@dataclass(frozen=True)
class ServiceManifest:
    """The fully-parsed, immutable manifest for a feature environment.

    Fields:
        session_prefix: Tmux session name prefix; sessions are named
            ``<session_prefix>-<env>``.
        env_file: Path to the env file, relative to the worktree root.
            ``None`` when not declared (env sourcing is skipped).
        layout_hook: Path to the optional bash layout hook, relative to the
            workspace root.  ``None`` when not declared.
        services: All declared services, in declaration order.
        status_urls: All declared status URLs, in declaration order.
        logs: Log-capture configuration.  Always present (defaulted); consumers
            need no null-guard.
        workspace_services: Services scoped to the shared ``<prefix>-workspace``
            singleton session, in declaration order.  Empty when none declared.
        workspace_layout_hook: Optional bash layout hook for the workspace
            session, relative to the workspace root.  ``None`` when not declared.
    """

    session_prefix: str
    env_file: str | None
    layout_hook: str | None
    services: tuple[Service, ...]
    status_urls: tuple[StatusUrl, ...]
    logs: LogConfig = field(default_factory=LogConfig)
    workspace_services: tuple[Service, ...] = ()
    workspace_layout_hook: str | None = None
