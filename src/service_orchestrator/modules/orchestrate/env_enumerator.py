"""Enumerate running env names from tmux session names.

The orchestrator has no persistent env registry — the only reliable source of
*currently running* envs is the tmux session list.  Sessions are named
``<prefix>-<env>``; this helper strips the prefix to recover the env name.

Extracted from ``env_cli._handle_status_all`` so both doors can reuse the
same enumeration logic without duplicating the prefix-strip arithmetic.
"""

from __future__ import annotations

from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository


def running_envs(tmux: ITmuxRepository, prefix: str) -> list[str]:
    """Return env names whose tmux session is currently running.

    Calls ``tmux.list_sessions()`` and returns every name that starts with
    ``<prefix>-``, with the ``<prefix>-`` prefix stripped.

    :param tmux: the tmux repository (real or fake).
    :param prefix: the resolved session-name prefix (e.g. ``"mp"``) — see
        ``SessionContextBuilder`` for how it is resolved (manifest
        ``session_prefix`` override, or the ``WINTER_SERVICE_PREFIX``
        environment variable).
    :returns: list of env names, in the order returned by ``list_sessions``.
    """
    sessions = tmux.list_sessions()
    needle = f"{prefix}-"
    return [s[len(needle) :] for s in sessions if s.startswith(needle)]
