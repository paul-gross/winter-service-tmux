from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

# Valid port expression: optional whitespace around "WINTER_PORT_BASE + <int>"
_PORT_EXPR_RE = re.compile(r"^\s*WINTER_PORT_BASE\s*\+\s*(\d+)\s*$")

# Duration form for HealthType.UPTIME's `target` — identical to the duration
# form winter-cli's `winter service logs --since`/`--until` accepts (mirrored
# here byte-for-byte rather than imported across repos, since
# winter-service-tmux is a separate package from winter-cli). Only the
# duration form applies; there is no RFC3339 absolute-timestamp form for an
# uptime target.
_UPTIME_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_UPTIME_DURATION_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_port_expression(s: str) -> int | None:
    """Parse a ``WINTER_PORT_BASE + <offset>`` expression and return the offset.

    Returns the integer offset when *s* matches the expression, or ``None``
    when it does not match.  The caller adds the offset to the resolved
    ``WINTER_PORT_BASE`` value to obtain the absolute port number.
    """
    m = _PORT_EXPR_RE.match(s)
    return int(m.group(1)) if m else None


def parse_uptime_duration(s: str) -> int | None:
    """Parse a ``HealthType.UPTIME`` health target into a duration in seconds.

    Accepts ``<N><unit>`` where unit is one of ``s``/``m``/``h``/``d``
    (1/60/3600/86400 seconds respectively) — e.g. ``"30s"``, ``"5m"``, ``"1h"``,
    ``"2d"``.  Returns ``None`` when *s* does not match this form.
    """
    m = _UPTIME_DURATION_RE.match(s.strip())
    if m is None:
        return None
    amount = int(m.group(1))
    unit = m.group(2)
    return amount * _UPTIME_DURATION_SECONDS[unit]


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
    LOG = "log"
    UPTIME = "uptime"


@dataclass(frozen=True)
class Health:
    """Optional readiness probe for a declared service.

    ``target`` may contain ``${VAR}`` placeholders resolved against the
    effective scope/env_file/service environment before the probe runs — EXCEPT
    for ``HealthType.LOG``, where ``target`` is a regular expression used
    VERBATIM (no ``${VAR}`` interpolation), so regex syntax is never mangled.
    For ``HealthType.UPTIME``, ``target`` is a
    duration (``parse_uptime_duration``) — the service is ``healthy`` once its
    measured process has been alive at least that long; there are no
    placeholders to interpolate in a duration.  ``timeout`` is seconds; ``None``
    means use the probe runner's default timeout (unused by ``log`` and
    ``uptime`` probes — neither performs I/O that can time out).
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
        cwd: Optional working directory for this service, as a scope-rooted
            relative path — resolved against the same base ``build_launch_line``
            already uses (the feature-env root for ``scope="project"``, the
            workspace root for ``scope="workspace"``).  A leading ``./`` is
            normalized away by the reader.  ``None`` (the default) means the
            pane ``cd``s to the scope root, matching prior behavior.  Supersedes
            the ``cd``-in-``cmd`` idiom.  The validator rejects an absolute
            value or one that normalizes outside the scope root (e.g.
            ``"../other"``).
        depends_on: Optional tuple of service patterns this service must wait
            for before ``winter service up`` launches it.  A bare pattern (no
            ``"/"``, e.g. ``"builder"``) is a same-scope reference — resolved
            at dispatch time against the current scope (the feature env, or
            ``"workspace"``).  A scope-qualified pattern whose scope segment
            EQUALS the current scope (e.g. ``"alpha/builder"`` declared by a
            service running in env ``"alpha"``) resolves to the identical
            poll target as the bare form and is treated identically for both
            local launch ordering and (when statically known, i.e. the
            ``"workspace"`` scope) validation — bare and self-qualified
            spellings are interchangeable, never one sequenced and the other
            not.  A scope-qualified pattern naming a DIFFERENT scope (e.g.
            ``"workspace/db"`` from a project-scope service) is resolved
            verbatim, letting a service depend on another provider's service
            (e.g. a docker workspace singleton); such cross-scope references
            are gated at launch time but are never locally sequenced and are
            not statically validated.  Every dependency is polled through the
            provider-agnostic ``winter service status <pattern> --json`` seam
            regardless of which provider owns it.  Empty tuple (the default)
            means no ordering constraint.  The validator rejects a same-scope
            pattern that is a self-reference, matches no declared service, or
            targets an interactive (empty-``cmd``) service with no health
            probe (a target that can never report ``state = "running"``, so
            the dependency would time out on every ``up``); it also rejects
            any same-scope ``depends_on`` cycle.  UNSUPPORTED TOPOLOGY: a
            cross-scope cycle formed entirely of scope-qualified patterns
            (e.g. ``env/A`` depends on ``workspace/B`` which depends on
            ``workspace/A``) is not statically detected by the validator and
            will hang at launch time (each side polls the other, and neither
            side's own scope ever calls ``_topological_order`` across the
            other's services) — do not declare one.
    """

    name: str
    target: Target
    cmd: str
    log: LogMode = LogMode.FILE
    health: Health | None = None
    startup: StartupPolicy | None = None
    port: int | str | None = None
    cwd: str | None = None
    depends_on: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze the mapping even when callers construct ``Service`` directly."""
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


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
