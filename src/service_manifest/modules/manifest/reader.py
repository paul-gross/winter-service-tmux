"""TOML manifest reader — parses config.toml + optional overlay → ServiceManifest.

All TOML parsing is confined here.  Callers receive a fully-constructed
``ServiceManifest`` or a ``ManifestError``; they never see ``tomllib`` or I/O
exceptions directly.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from service_manifest.core.filesystem import IFilesystemReader
from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import (
    Health,
    HealthType,
    LogConfig,
    LogMode,
    Service,
    ServiceManifest,
    StartupPolicy,
    Target,
)

# File names within the config dir (resolved by the locator / CLI before calling read()).
# The entrypoint is workflow/orchestrate, which delegates to the locator for the dir.
_COMMITTED_NAME = "config.toml"
_LOCAL_NAME = "config.local.toml"

# Allowed values for a ``[[service]]`` entry's config-only ``scope`` discriminator.
_VALID_SCOPES = ("project", "workspace")


class ManifestReader:
    """Reads ``config.toml`` (+ optional ``config.local.toml`` overlay)
    from a config directory and constructs a ``ServiceManifest``.

    ALL TOML parsing is confined here.

    Overlay merge semantics:

    * **Scalars** (``session_prefix``, ``env_file``, ``layout_hook``,
      ``workspace_layout_hook``): the overlay value replaces the committed
      value when present.
    * **``[[service]]``**: merged keyed by ``name``.  An overlay service whose
      ``name`` matches an existing committed service *overrides* it in place
      (preserving position).  A new name is *appended* after all committed
      services.  An entry's ``scope`` (``"project"`` default, or ``"workspace"``)
      travels inside the entry, so an override carries/sets its own scope.
    * **``[[status.url]]``**: silently ignored — this feature has been removed.
    """

    def __init__(self, fs: IFilesystemReader) -> None:
        self._fs = fs

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(self, config_dir: Path) -> ServiceManifest:
        """Parse the committed manifest (+ overlay if present) and return a
        ``ServiceManifest``.

        Args:
            config_dir: Directory containing ``config.toml`` (committed) and
                optionally ``config.local.toml`` (per-machine overlay).
                Resolved by the caller — typically ``IWorkspaceLocator.config_dir()``
                or the ``WINTER_EXT_CONFIG_DIR`` env var.

        Raises ``ManifestError`` on: committed file absent, file unreadable,
        malformed TOML, a declared ``session_prefix`` that is not a non-empty
        string, a ``[[service]]`` missing ``name`` or ``target``, or a
        ``target`` that cannot be split into two integers.
        """
        committed_path = config_dir / _COMMITTED_NAME
        local_path = config_dir / _LOCAL_NAME

        committed = self._load_toml(committed_path, required=True)
        local = self._load_toml(local_path, required=False)

        merged = self._merge(committed, local)
        return self._build(merged)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_toml(self, path: Path, *, required: bool) -> dict:  # type: ignore[type-arg]
        """Read and parse one TOML file.

        Returns an empty dict when *path* does not exist and *required* is
        ``False``.  Raises ``ManifestError`` when *required* is ``True`` and
        the file is absent, or when the file cannot be read or parsed.
        """
        if not self._fs.exists(path):
            if required:
                raise ManifestError(f"committed manifest not found: {path}") from None
            return {}

        try:
            text = self._fs.read_text(path)
        except OSError as exc:
            raise ManifestError(f"cannot read manifest {path}: {exc}") from exc

        try:
            return tomllib.loads(text)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ManifestError(f"malformed TOML in {path}: {exc}") from exc

    @staticmethod
    def _merge_keyed(
        committed_list: list[dict],  # type: ignore[type-arg]
        overlay_list: list[dict],  # type: ignore[type-arg]
        key: str,
        nested_tables: tuple[str, ...] = (),
    ) -> list[dict]:  # type: ignore[type-arg]
        """Merge *overlay_list* on top of *committed_list* keyed by *key*.

        An overlay entry whose *key* matches an existing committed entry
        *overrides* it in place (dict fields are merged so partial overrides
        work).  Tables named in *nested_tables* are merged by their own keys
        instead of being replaced wholesale.  An entry with a new *key* value
        is *appended* after all committed entries.  Entries without *key* are
        always appended.
        """
        key_to_idx: dict[str, int] = {}
        for i, entry in enumerate(committed_list):
            if key in entry:
                key_to_idx[entry[key]] = i

        result: list[dict] = list(committed_list)  # type: ignore[type-arg]
        for overlay_entry in overlay_list:
            entry_key = overlay_entry.get(key)
            if entry_key is not None and entry_key in key_to_idx:
                idx = key_to_idx[entry_key]
                merged = {**result[idx], **overlay_entry}
                for table in nested_tables:
                    committed_table = result[idx].get(table)
                    overlay_table = overlay_entry.get(table)
                    if isinstance(committed_table, dict) and isinstance(overlay_table, dict):
                        merged[table] = {**committed_table, **overlay_table}
                result[idx] = merged
            else:
                result.append(overlay_entry)
                if entry_key is not None:
                    key_to_idx[entry_key] = len(result) - 1

        return result

    @staticmethod
    def _merge(committed: dict, local: dict) -> dict:  # type: ignore[type-arg]
        """Merge *local* overlay on top of *committed* document.

        Scalar fields are replaced by the overlay value when present.
        ``[[service]]`` uses keyed override-or-append (the single ``[[service]]``
        list carries per-entry ``scope``, so an overlay override keeps/sets its
        own scope).  ``[logs]`` merges per-key — an overlay ``[logs]`` replaces
        only the keys it sets, keeping committed values for the rest.  All other
        top-level keys are replaced wholesale by the overlay value.
        ``[[status.url]]`` entries in the overlay are silently ignored.
        """
        if not local:
            return committed

        result: dict = dict(committed)  # type: ignore[type-arg]

        # --- scalars ---
        for key in ("session_prefix", "env_file", "layout_hook", "workspace_layout_hook"):
            if key in local:
                result[key] = local[key]

        # --- [logs]: per-key merge so a local overlay can override one key only ---
        if "logs" in local:
            committed_logs: dict = dict(committed.get("logs", {}))  # type: ignore[type-arg]
            committed_logs.update(local["logs"])
            result["logs"] = committed_logs

        # --- [[service]]: keyed by "name", override-or-append ---
        # Normalise command→cmd in every entry BEFORE the merge so that an
        # overlay using the deprecated alias correctly overwrites the committed
        # canonical key (and vice-versa).
        if "service" in local:
            _norm = ManifestReader._normalize_cmd_key
            result["service"] = ManifestReader._merge_keyed(
                [_norm(e) for e in committed.get("service", [])],
                [_norm(e) for e in local["service"]],
                "name",
                nested_tables=("env",),
            )

        return result

    @staticmethod
    def _parse_target(raw: str, service_name: str) -> Target:
        """Parse a ``"<window>.<pane>"`` string into a ``Target``.

        Raises ``ManifestError`` naming the offending service when the string
        cannot be split into exactly two non-negative integers.
        """
        parts = raw.split(".")
        if len(parts) != 2:
            raise ManifestError(
                f"service '{service_name}': malformed target '{raw}' — expected '<window>.<pane>' (e.g. '0.1')"
            )
        w_raw, p_raw = parts
        try:
            window = int(w_raw)
            pane = int(p_raw)
        except ValueError as exc:
            raise ManifestError(
                f"service '{service_name}': malformed target '{raw}' — window and pane must be integers"
            ) from exc
        return Target(window=window, pane=pane)

    @staticmethod
    def _normalize_cmd_key(entry: dict) -> dict:  # type: ignore[type-arg]
        """Normalize the ``command`` alias to the canonical ``cmd`` key in one entry.

        When only ``command`` is present the key is renamed to ``cmd`` and a
        deprecation warning is emitted to stderr naming the service.  When both
        ``cmd`` and ``command`` are present, ``cmd`` wins and ``command`` is
        dropped (no warning — the author is already using the canonical key).
        Entries that already use only ``cmd``, or neither key, are returned
        unchanged (the dict is copied only when a change is needed).
        """
        if "command" not in entry:
            return entry
        if "cmd" in entry:
            # cmd is canonical; drop the stale alias silently.
            result = dict(entry)
            del result["command"]
            return result
        # Only "command" is present — rename with a deprecation warning.
        name = entry.get("name", "<unknown>")
        sys.stderr.write(
            f"[winter-service-tmux] deprecation: service '{name}' uses 'command'; "
            "rename it to 'cmd' — 'command' will be removed in a future release\n"
        )
        sys.stderr.flush()
        result = dict(entry)
        result["cmd"] = result.pop("command")
        return result

    @staticmethod
    def _parse_services(
        raw_services: list[dict],  # type: ignore[type-arg]
    ) -> list[tuple[Service, str]]:
        """Parse ``[[service]]`` entries into ``(Service, scope)`` pairs.

        Each entry is parsed into a typed ``Service`` and its config-only
        ``scope`` discriminator is validated alongside it (``"project"`` default,
        or ``"workspace"``).  ``scope`` is returned NEXT TO the ``Service`` rather
        than stored on it — the runtime model is scope-agnostic — so the caller
        partitions the already-typed pairs (see ``_build``).

        Raises ``ManifestError`` on a missing/invalid field or an unknown
        ``scope``, naming the offending service.

        Entries are expected to have been normalised by ``_normalize_cmd_key``
        before reaching this method — only the canonical ``cmd`` key is checked.
        """
        parsed: list[tuple[Service, str]] = []
        for i, raw in enumerate(raw_services):
            name = raw.get("name")
            if not name:
                raise ManifestError(f"[[service]] entry #{i} is missing required field 'name'")
            if not isinstance(name, str):
                raise ManifestError(f"[[service]] entry #{i}: name must be a string, got {type(name).__name__}")
            target_str = raw.get("target")
            if target_str is None:
                raise ManifestError(f"service '{name}' is missing required field 'target'")
            if not isinstance(target_str, str):
                raise ManifestError(
                    f"service '{name}': target must be a quoted string like \"0.1\", got {type(target_str).__name__}"
                )
            target = ManifestReader._parse_target(target_str, name)
            if "cmd" in raw:
                cmd_raw = raw["cmd"]
                if not isinstance(cmd_raw, str):
                    raise ManifestError(f"service '{name}': cmd must be a string, got {type(cmd_raw).__name__}")
            else:
                cmd_raw = ""
            cmd: str = cmd_raw
            log_raw = raw.get("log", LogMode.FILE.value)
            _allowed = [m.value for m in LogMode]
            if not isinstance(log_raw, str):
                raise ManifestError(
                    f"service '{name}': 'log' must be a string "
                    f"({', '.join(repr(v) for v in _allowed)}), "
                    f"got {type(log_raw).__name__}"
                )
            if log_raw not in _allowed:
                raise ManifestError(
                    f"service '{name}': 'log' value {log_raw!r} is not valid; "
                    f"allowed values are {', '.join(repr(v) for v in _allowed)}"
                )
            log_mode = LogMode(log_raw)
            health_raw = raw.get("health")
            health: Health | None = None
            if health_raw is not None:
                if not isinstance(health_raw, dict):
                    raise ManifestError(f"service '{name}': 'health' must be a table, got {type(health_raw).__name__}")
                type_raw = health_raw.get("type")
                if not isinstance(type_raw, str):
                    raise ManifestError(f"service '{name}': health.type must be a string")
                target_raw = health_raw.get("target")
                if not isinstance(target_raw, str):
                    raise ManifestError(f"service '{name}': health.target must be a string")
                timeout_raw = health_raw.get("timeout")
                timeout: float | None = None
                if timeout_raw is not None:
                    if not isinstance(timeout_raw, int | float):
                        raise ManifestError(
                            f"service '{name}': health.timeout must be a number, got {type(timeout_raw).__name__}"
                        )
                    timeout = float(timeout_raw)
                try:
                    health_type = HealthType(type_raw)
                except ValueError as exc:
                    allowed = ", ".join(repr(t.value) for t in HealthType)
                    raise ManifestError(
                        f"service '{name}': health.type value {type_raw!r} is not valid; allowed values are {allowed}"
                    ) from exc
                health = Health(type=health_type, target=target_raw, timeout=timeout)
            startup_raw = raw.get("startup")
            startup: StartupPolicy | None = None
            if startup_raw is not None:
                if not isinstance(startup_raw, dict):
                    raise ManifestError(
                        f"service '{name}': 'startup' must be a table, got {type(startup_raw).__name__}"
                    )
                startup_kwargs: dict = {}  # type: ignore[type-arg]
                retries_raw = startup_raw.get("retries")
                if retries_raw is not None:
                    if not isinstance(retries_raw, int):
                        raise ManifestError(
                            f"service '{name}': startup.retries must be an integer, got {type(retries_raw).__name__}"
                        )
                    startup_kwargs["retries"] = retries_raw
                retry_delay_raw = startup_raw.get("retry_delay")
                if retry_delay_raw is not None:
                    if not isinstance(retry_delay_raw, int | float):
                        raise ManifestError(
                            f"service '{name}': startup.retry_delay must be a number, "
                            f"got {type(retry_delay_raw).__name__}"
                        )
                    startup_kwargs["retry_delay"] = float(retry_delay_raw)
                startup = StartupPolicy(**startup_kwargs)
            scope = raw.get("scope", "project")
            if scope not in _VALID_SCOPES:
                raise ManifestError(
                    f"service '{name}': invalid scope {scope!r}; "
                    f"allowed values are {', '.join(repr(s) for s in _VALID_SCOPES)}"
                )
            port_raw = raw.get("port")
            port: int | str | None = None
            if port_raw is not None:
                if isinstance(port_raw, bool):
                    raise ManifestError(f"service '{name}': 'port' must be an integer or a string expression, got bool")
                if isinstance(port_raw, int | str):
                    port = port_raw
                else:
                    raise ManifestError(
                        f"service '{name}': 'port' must be an integer or a string expression "
                        f"(e.g. 'WINTER_PORT_BASE + 10'), got {type(port_raw).__name__}"
                    )
            cwd_raw = raw.get("cwd")
            cwd: str | None = None
            if cwd_raw is not None:
                if not isinstance(cwd_raw, str):
                    raise ManifestError(f"service '{name}': cwd must be a string, got {type(cwd_raw).__name__}")
                cwd = cwd_raw[2:] if cwd_raw.startswith("./") else cwd_raw
            depends_on_raw = raw.get("depends_on")
            depends_on: tuple[str, ...] = ()
            if depends_on_raw is not None:
                if not isinstance(depends_on_raw, list):
                    raise ManifestError(
                        f"service '{name}': 'depends_on' must be a list of strings, got {type(depends_on_raw).__name__}"
                    )
                for item in depends_on_raw:
                    if not isinstance(item, str):
                        raise ManifestError(
                            f"service '{name}': 'depends_on' entries must be strings, got {type(item).__name__}"
                        )
                depends_on = tuple(depends_on_raw)
            env_raw = raw.get("env")
            env: dict[str, str] = {}
            if env_raw is not None:
                if not isinstance(env_raw, dict):
                    raise ManifestError(f"service '{name}': 'env' must be a table, got {type(env_raw).__name__}")
                for key, value in env_raw.items():
                    if not isinstance(value, str):
                        raise ManifestError(f"service '{name}': env.{key} must be a string, got {type(value).__name__}")
                    env[key] = value
            svc = Service(
                name=name,
                target=target,
                cmd=cmd,
                log=log_mode,
                health=health,
                startup=startup,
                port=port,
                cwd=cwd,
                depends_on=depends_on,
                env=env,
            )
            parsed.append((svc, scope))
        return parsed

    @staticmethod
    def _build(doc: dict) -> ServiceManifest:  # type: ignore[type-arg]
        """Construct a ``ServiceManifest`` from the merged TOML document.

        Raises ``ManifestError`` on missing required fields or unparseable values.
        """
        # --- optional scalar override ---
        # Absent (None) is the default and recommended setting: the prefix is
        # then resolved entirely from WINTER_SERVICE_PREFIX by
        # SessionContextBuilder at dispatch time. When declared, it must be a
        # non-empty string.
        session_prefix: str | None = doc.get("session_prefix")
        if session_prefix is not None and (not isinstance(session_prefix, str) or not session_prefix):
            raise ManifestError("'session_prefix' must be a non-empty string")

        env_file: str | None = doc.get("env_file") or None
        layout_hook: str | None = doc.get("layout_hook") or None
        workspace_layout_hook: str | None = doc.get("workspace_layout_hook") or None

        # --- [[service]] — parse once into (Service, scope) pairs, then partition ---
        # Normalise command→cmd here so _parse_services only ever sees the
        # canonical key (covers the no-overlay path; the overlay path also
        # normalises but a second pass is idempotent and cheap).
        _norm = ManifestReader._normalize_cmd_key
        raw_service_entries = [_norm(e) for e in doc.get("service", [])]
        parsed_services = ManifestReader._parse_services(raw_service_entries)
        services = [svc for svc, scope in parsed_services if scope == "project"]
        workspace_services = [svc for svc, scope in parsed_services if scope == "workspace"]

        # --- [logs] ---
        raw_logs: dict = doc.get("logs", {})  # type: ignore[type-arg]
        _log_int_fields = ("rotate_size_bytes", "max_rotations", "retention_seconds")
        log_kwargs: dict = {}  # type: ignore[type-arg]
        for field_name in _log_int_fields:
            if field_name in raw_logs:
                val = raw_logs[field_name]
                if not isinstance(val, int):
                    raise ManifestError(f"[logs] '{field_name}' must be an integer, got {type(val).__name__}")
                log_kwargs[field_name] = val
        logs = LogConfig(**log_kwargs)

        return ServiceManifest(
            session_prefix=session_prefix,
            env_file=env_file,
            layout_hook=layout_hook,
            services=tuple(services),
            logs=logs,
            workspace_services=tuple(workspace_services),
            workspace_layout_hook=workspace_layout_hook,
        )
