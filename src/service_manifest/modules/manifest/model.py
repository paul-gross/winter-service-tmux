from __future__ import annotations

from dataclasses import dataclass


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
    """

    name: str
    target: Target
    command: str


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
    """

    session_prefix: str
    env_file: str | None
    layout_hook: str | None
    services: tuple[Service, ...]
    status_urls: tuple[StatusUrl, ...]
