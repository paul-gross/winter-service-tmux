"""Pattern selection and expansion service for the orchestrator CLI door.

Owns all logic for resolving ``<env>/<svc>`` glob patterns into concrete
``env → [service_names]`` mappings, including workspace-pattern splitting,
manifest context seeding, and dead-pattern detection.

This keeps ``cli.py`` free of pattern-arithmetic internals and makes the
expansion logic unit-testable with fake seams.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from service_manifest.modules.manifest.errors import ManifestError
from service_orchestrator.modules.orchestrate.env_enumerator import running_envs
from service_orchestrator.modules.orchestrate.pattern_match import matches_any_pattern
from service_orchestrator.modules.orchestrate.session_context_builder import (
    WORKSPACE_TARGET,
    SessionContextBuilder,
    build_for_target,
)
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository


def _is_glob(segment: str) -> bool:
    """Return True when *segment* contains fnmatch wildcard characters."""
    return "*" in segment or "?" in segment or "[" in segment


def _candidate_envs_for_pattern(pattern: str, all_running: list[str]) -> list[str]:
    """Return the list of candidate env names to check for *pattern*.

    A bare pattern (no ``/``) is treated as ``<pattern>/*`` — the env segment
    is the whole token.  If that env segment is a concrete name, return it as
    a singleton; if it is a glob, filter *all_running* against it.
    """
    env_seg = pattern if "/" not in pattern else pattern.split("/", 1)[0]

    if _is_glob(env_seg):
        return [e for e in all_running if fnmatch.fnmatchcase(e, env_seg)]
    return [env_seg]


class SelectorService:
    """Expand ``<env>/<svc>`` glob patterns into concrete selections.

    Consumes the existing ``ITmuxRepository`` and ``SessionContextBuilder``
    seams (no new raw I/O) so the existing fake implementations cover all tests
    without modification.

    Construction: ``SelectorService(container.tmux, container.session_context_builder)``.
    """

    def __init__(self, tmux: ITmuxRepository, builder: SessionContextBuilder) -> None:
        self._tmux = tmux
        self._builder = builder

    def split_workspace_patterns(
        self,
        patterns: list[str],
    ) -> tuple[list[str], list[str]]:
        """Partition *patterns* into workspace patterns and non-workspace patterns.

        A pattern is a workspace pattern when its env segment is EXACTLY
        ``WORKSPACE_TARGET`` (``"workspace"``).  Glob env-segments like ``work*``
        are NOT workspace patterns — they travel through the normal engine (where
        they will simply never match an env because the workspace session is
        intercepted at the enumeration layer).

        Returns:
            ``(workspace_pats, env_pats)`` — both are fresh lists.
        """
        workspace_pats: list[str] = []
        env_pats: list[str] = []
        for pat in patterns:
            env_seg = pat if "/" not in pat else pat.split("/", 1)[0]
            if env_seg == WORKSPACE_TARGET:
                workspace_pats.append(pat)
            else:
                env_pats.append(pat)
        return workspace_pats, env_pats

    def read_manifest_context(
        self,
        patterns: list[str],
        workspace_root: Path | None,
    ) -> tuple[str, list[str]] | None:
        """Read session_prefix and service names from the manifest.

        Tries to derive a candidate env from a concrete env segment in the
        patterns; falls back to any running tmux session.  Returns
        ``(prefix, service_names)`` or ``None`` on failure.

        Seed selection prefers a concrete env-segment from a pattern; else
        the first **non-workspace** running tmux session; else workspace only
        if it is the sole session.

        ``OrchestratorError`` (e.g. the session-prefix resolution failure raised
        by ``_resolve_session_prefix`` when neither a manifest override nor
        ``WINTER_SERVICE_PREFIX`` is set) is deliberately NOT caught here — it
        propagates to the caller so its specific diagnostic reaches the user
        instead of being folded into a generic "no services matched" message.
        Only ``ManifestError``/``OSError`` (genuinely missing/unreadable
        manifest) are swallowed into ``None``.
        """
        candidate_env: str | None = None

        # Prefer a concrete env from a pattern
        for pat in patterns:
            seg = pat if "/" not in pat else pat.split("/", 1)[0]
            if not _is_glob(seg):
                candidate_env = seg
                break

        # Fall back to any running session.
        # Prefer a non-workspace seed: if the workspace session is listed first but
        # at least one env session also exists, skip the workspace entry and seed
        # from an env session instead.  Only fall back to workspace if it is the
        # sole session, mirroring the same guard in the 0-pattern status path.
        if candidate_env is None:
            sessions = self._tmux.list_sessions()
            candidate_env = None
            for sess in sessions:
                if "-" not in sess:
                    continue
                env_name = sess.split("-", 1)[1]
                if env_name != WORKSPACE_TARGET:
                    candidate_env = env_name
                    break
            # Only use workspace as the seed when it is the only session available.
            if candidate_env is None:
                for sess in sessions:
                    if "-" in sess:
                        candidate_env = sess.split("-", 1)[1]
                        break

        if candidate_env is None:
            return None

        try:
            ctx = build_for_target(self._builder, candidate_env, workspace_root=workspace_root)
            return (
                ctx.session_prefix,
                [svc.name for svc in ctx.services],
            )
        except (ManifestError, OSError):
            return None

    def expand_env_patterns(
        self,
        patterns: list[str],
        services_list: list[str],
        prefix: str,
        workspace_root: Path | None,
    ) -> tuple[dict[str, list[str]], list[str]]:
        """Expand ``patterns`` against manifest services and running envs.

        The manifest's service names are pre-loaded by the caller (``services_list``)
        and the session prefix (``prefix``) is also provided, avoiding a second
        manifest read.

        Returns:
            ``env_services``: ordered mapping env → list of matched service names
                (in manifest order, preserving only the matched subset).
            ``dead_patterns``: patterns that matched zero (env, svc) pairs.
        """
        # Lazily enumerate running envs once when we encounter a glob env-segment.
        live_envs: list[str] | None = None

        env_services: dict[str, list[str]] = {}
        matched_patterns: set[str] = set()

        for pat in patterns:
            env_seg = pat if "/" not in pat else pat.split("/", 1)[0]
            if _is_glob(env_seg) and live_envs is None:
                live_envs = running_envs(self._tmux, prefix)
            candidate_envs = _candidate_envs_for_pattern(pat, live_envs if live_envs is not None else [])

            for env in candidate_envs:
                for svc_name in services_list:
                    if matches_any_pattern(env, svc_name, [pat]):
                        env_services.setdefault(env, [])
                        if svc_name not in env_services[env]:
                            env_services[env].append(svc_name)
                        matched_patterns.add(pat)

        dead_patterns = [p for p in patterns if p not in matched_patterns]
        return env_services, dead_patterns

    def expand_workspace_patterns(
        self,
        workspace_pats: list[str],
        ws_services: list[str],
    ) -> tuple[list[str], list[str]]:
        """Expand workspace patterns against a list of workspace service names.

        Each pattern's svc-segment (the part after ``/``, or ``*`` for a bare
        ``workspace`` token) is matched against *ws_services* with
        ``fnmatch.fnmatchcase``.

        Returns:
            ``(matched_names, dead_patterns)`` — matched is deduped in order;
            dead contains patterns that matched nothing.
        """
        matched: list[str] = []
        dead: list[str] = []
        for pat in workspace_pats:
            svc_glob = pat.split("/", 1)[1] if "/" in pat else "*"
            hits = [n for n in ws_services if fnmatch.fnmatchcase(n, svc_glob)]
            if not hits:
                dead.append(pat)
            for n in hits:
                if n not in matched:
                    matched.append(n)
        return matched, dead
