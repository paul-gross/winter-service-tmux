"""Extension-manifest merger — reads ``WINTER_SERVICE_MANIFEST`` and merges services.

When winter-cli passes aggregated extension-declared service definitions to the
tmux orchestrator via the ``WINTER_SERVICE_MANIFEST`` environment variable, this
module reads that TOML file and merges the declared services into the
``ServiceManifest`` that was loaded from the committed ``config.toml``.

Contract
--------
- ``ExtManifestMerger.read_and_merge`` accepts a ``ServiceManifest`` (already
  loaded from ``config.toml``) and a path to the ext-service manifest TOML file.
  It returns a new ``ServiceManifest`` with any extension-declared services
  appended to the appropriate scope list (``services`` for
  ``scope = "feature-environment"``, ``workspace_services`` for
  ``scope = "workspace"``).
- Services whose ``target`` field is empty are SKIPPED with a log warning — the
  tmux provider requires a pane address.  Providers that cannot run a service
  simply ignore it.
- Services whose ``name`` collides with an already-declared service (from
  ``config.toml``) are also skipped with a warning (collision resolution
  happened upstream in winter-cli; this is a safety net for partial injections).
- Malformed TOML in the ext-manifest file is logged as a warning; the original
  manifest is returned unmodified (graceful degradation).

This module is a cold-path import (only loaded when ``WINTER_SERVICE_MANIFEST``
is set in the environment).
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from service_manifest.modules.manifest.model import (
    Service,
    ServiceManifest,
    Target,
)

logger = logging.getLogger(__name__)

# Keys that are recognised in the ext-manifest [[service]] entries.
# ``source`` carries attribution; ``ports`` is for future providers.
# Both ``cmd`` (canonical) and ``command`` (deprecated alias) are accepted.
_EXT_KNOWN_KEYS = frozenset({"name", "scope", "source", "cmd", "command", "image", "target", "ports"})

_VALID_SCOPES = frozenset({"workspace", "feature-environment"})


class ExtManifestMerger:
    """Reads a ``WINTER_SERVICE_MANIFEST`` TOML file and merges services.

    All failures (missing file, bad TOML, unknown fields, missing target) are
    logged as warnings — graceful degradation so a bad extension declaration
    does not break ``winter service up``.
    """

    def read_and_merge(self, base: ServiceManifest, ext_path: Path) -> ServiceManifest:
        """Return a new ``ServiceManifest`` with extension services merged in.

        ``base`` is the manifest loaded from ``config.toml``.
        ``ext_path`` is the path to the ``WINTER_SERVICE_MANIFEST`` TOML file.

        Services that cannot be started (missing target, unknown scope, or name
        collision with an existing service) are skipped with a warning.
        """
        try:
            text = ext_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("ext-manifest: cannot read %s: %s", ext_path, exc)
            return base

        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            logger.warning("ext-manifest: malformed TOML in %s: %s", ext_path, exc)
            return base

        raw_services = doc.get("service", [])
        if not isinstance(raw_services, list):
            logger.warning("ext-manifest: [[service]] in %s must be a list", ext_path)
            return base

        # Build existing name sets for collision detection.
        existing_names: set[str] = {s.name for s in base.services} | {s.name for s in base.workspace_services}

        extra_env: list[Service] = []
        extra_ws: list[Service] = []

        for i, raw in enumerate(raw_services):
            if not isinstance(raw, dict):
                logger.warning("ext-manifest: [[service]][%d] is not a table, skipping", i)
                continue

            name = raw.get("name", "")
            if not isinstance(name, str) or not name:
                logger.warning("ext-manifest: [[service]][%d] missing 'name', skipping", i)
                continue

            if name in existing_names:
                logger.warning(
                    "ext-manifest: service %r already declared in config.toml, skipping ext def",
                    name,
                )
                continue

            scope = raw.get("scope", "feature-environment")
            if scope not in _VALID_SCOPES:
                logger.warning("ext-manifest: service %r has unknown scope %r, skipping", name, scope)
                continue

            target_str = raw.get("target", "")
            if not isinstance(target_str, str) or not target_str:
                logger.warning(
                    "ext-manifest: service %r has no 'target' field — "
                    'tmux provider requires a pane address (e.g. target = "2.0"), skipping',
                    name,
                )
                continue

            target = self._parse_target(name, target_str)
            if target is None:
                continue

            if "cmd" in raw:
                cmd = raw["cmd"]
                if not isinstance(cmd, str):
                    logger.warning("ext-manifest: service %r 'cmd' is not a string, skipping", name)
                    continue
            elif "command" in raw:
                logger.warning(
                    "ext-manifest: service %r uses 'command'; rename it to 'cmd' — "
                    "'command' will be removed in a future release",
                    name,
                )
                cmd = raw["command"]
                if not isinstance(cmd, str):
                    logger.warning("ext-manifest: service %r 'cmd' is not a string, skipping", name)
                    continue
            else:
                cmd = ""

            # Extension manifests do not carry service-specific environment
            # mappings; keep the model's explicit empty default here.
            svc = Service(name=name, target=target, cmd=cmd, env={})
            existing_names.add(name)

            if scope == "workspace":
                extra_ws.append(svc)
            else:
                extra_env.append(svc)

        if not extra_env and not extra_ws:
            return base

        return ServiceManifest(
            session_prefix=base.session_prefix,
            env_file=base.env_file,
            layout_hook=base.layout_hook,
            services=base.services + tuple(extra_env),
            logs=base.logs,
            workspace_services=base.workspace_services + tuple(extra_ws),
            workspace_layout_hook=base.workspace_layout_hook,
        )

    @staticmethod
    def _parse_target(name: str, raw: str) -> Target | None:
        """Parse ``"<window>.<pane>"`` into a ``Target``.

        Returns ``None`` and logs a warning on malformed input.
        """
        parts = raw.split(".")
        if len(parts) != 2:
            logger.warning(
                "ext-manifest: service %r: malformed target %r — expected '<window>.<pane>', skipping",
                name,
                raw,
            )
            return None
        try:
            window = int(parts[0])
            pane = int(parts[1])
        except ValueError:
            logger.warning(
                "ext-manifest: service %r: target %r — window and pane must be integers, skipping",
                name,
                raw,
            )
            return None
        return Target(window=window, pane=pane)
