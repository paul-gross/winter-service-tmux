"""TOML manifest reader — parses setup-tmux.toml + optional overlay → ServiceManifest.

All TOML parsing is confined here.  Callers receive a fully-constructed
``ServiceManifest`` or a ``ManifestError``; they never see ``tomllib`` or I/O
exceptions directly.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from service_manifest.core.filesystem import IFilesystemReader
from service_manifest.modules.manifest.errors import ManifestError
from service_manifest.modules.manifest.model import LogConfig, LogMode, Service, ServiceManifest, StatusUrl, Target

# Relative path from the workspace/worktree root to the manifest directory.
# Mirrors the bash resolution in winter-service-tmux.sh:
#   local project_dir="$1/ai/project"
_MANIFEST_DIR = Path("ai") / "project"
_COMMITTED_NAME = "setup-tmux.toml"
_LOCAL_NAME = "setup-tmux.local.toml"


class ManifestReader:
    """Reads ``setup-tmux.toml`` (+ optional ``setup-tmux.local.toml`` overlay)
    from a workspace root directory and constructs a ``ServiceManifest``.

    ALL TOML parsing is confined here.

    Overlay merge semantics (mirrors the bash committed→local overlay in
    ``winter-service-tmux.sh``):

    * **Scalars** (``session_prefix``, ``env_file``, ``layout_hook``): the
      overlay value replaces the committed value when present.
    * **``[[service]]``**: merged keyed by ``name``.  An overlay service whose
      ``name`` matches an existing committed service *overrides* it in place
      (preserving position).  A new name is *appended* after all committed
      services.
    * **``[[status.url]]``**: merged keyed by ``label`` — same
      override-or-append rule as ``[[service]]``.
    """

    def __init__(self, fs: IFilesystemReader) -> None:
        self._fs = fs

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(self, workspace_root: Path) -> ServiceManifest:
        """Parse the committed manifest (+ overlay if present) and return a
        ``ServiceManifest``.

        Raises ``ManifestError`` on: committed file absent, file unreadable,
        malformed TOML, missing required ``session_prefix``, a ``[[service]]``
        missing ``name`` or ``target``, or a ``target`` that cannot be split
        into two integers.
        """
        project_dir = workspace_root / _MANIFEST_DIR
        committed_path = project_dir / _COMMITTED_NAME
        local_path = project_dir / _LOCAL_NAME

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
                raise ManifestError(
                    f"committed manifest not found: {path}"
                ) from None
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
    def _merge(committed: dict, local: dict) -> dict:  # type: ignore[type-arg]
        """Merge *local* overlay on top of *committed* document.

        Scalar fields are replaced by the overlay value when present.
        ``[[service]]`` and ``[[status.url]]`` use keyed override-or-append.
        ``[logs]`` merges per-key — an overlay ``[logs]`` replaces only the keys
        it sets, keeping committed values for the rest.
        All other top-level keys are replaced wholesale by the overlay value.
        """
        if not local:
            return committed

        result: dict = dict(committed)  # type: ignore[type-arg]

        # --- scalars ---
        for key in ("session_prefix", "env_file", "layout_hook"):
            if key in local:
                result[key] = local[key]

        # --- [logs]: per-key merge so a local overlay can override one key only ---
        if "logs" in local:
            committed_logs: dict = dict(committed.get("logs", {}))  # type: ignore[type-arg]
            committed_logs.update(local["logs"])
            result["logs"] = committed_logs

        # --- [[service]]: keyed by "name", override-or-append ---
        if "service" in local:
            committed_services: list[dict] = list(committed.get("service", []))  # type: ignore[type-arg]
            overlay_services: list[dict] = local["service"]  # type: ignore[type-arg]

            # Index committed services by name (preserve last-wins for duplicates
            # in committed itself, consistent with bash arrays).
            name_to_idx: dict[str, int] = {}
            for i, svc in enumerate(committed_services):
                if "name" in svc:
                    name_to_idx[svc["name"]] = i

            merged_services: list[dict] = list(committed_services)  # type: ignore[type-arg]
            for overlay_svc in overlay_services:
                name = overlay_svc.get("name")
                if name is not None and name in name_to_idx:
                    # Override in place — merge dict fields so partial overrides work.
                    idx = name_to_idx[name]
                    merged_services[idx] = {**merged_services[idx], **overlay_svc}
                else:
                    merged_services.append(overlay_svc)
                    if name is not None:
                        name_to_idx[name] = len(merged_services) - 1

            result["service"] = merged_services

        # --- [[status.url]]: keyed by "label", override-or-append ---
        if "status" in local and "url" in local.get("status", {}):
            committed_urls: list[dict] = list(  # type: ignore[type-arg]
                committed.get("status", {}).get("url", [])
            )
            overlay_urls: list[dict] = local["status"]["url"]  # type: ignore[type-arg]

            label_to_idx: dict[str, int] = {}
            for i, u in enumerate(committed_urls):
                if "label" in u:
                    label_to_idx[u["label"]] = i

            merged_urls: list[dict] = list(committed_urls)  # type: ignore[type-arg]
            for overlay_url in overlay_urls:
                label = overlay_url.get("label")
                if label is not None and label in label_to_idx:
                    idx = label_to_idx[label]
                    merged_urls[idx] = {**merged_urls[idx], **overlay_url}
                else:
                    merged_urls.append(overlay_url)
                    if label is not None:
                        label_to_idx[label] = len(merged_urls) - 1

            result.setdefault("status", {})["url"] = merged_urls

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
                f"service '{service_name}': malformed target '{raw}' "
                f"— expected '<window>.<pane>' (e.g. '0.1')"
            )
        w_raw, p_raw = parts
        try:
            window = int(w_raw)
            pane = int(p_raw)
        except ValueError as exc:
            raise ManifestError(
                f"service '{service_name}': malformed target '{raw}' "
                f"— window and pane must be integers"
            ) from exc
        return Target(window=window, pane=pane)

    @staticmethod
    def _build(doc: dict) -> ServiceManifest:  # type: ignore[type-arg]
        """Construct a ``ServiceManifest`` from the merged TOML document.

        Raises ``ManifestError`` on missing required fields or unparseable values.
        """
        # --- required scalar ---
        if "session_prefix" not in doc:
            raise ManifestError("manifest is missing required field 'session_prefix'")
        session_prefix = doc["session_prefix"]
        if not isinstance(session_prefix, str) or not session_prefix:
            raise ManifestError("'session_prefix' must be a non-empty string")

        env_file: str | None = doc.get("env_file") or None
        layout_hook: str | None = doc.get("layout_hook") or None

        # --- [[service]] ---
        raw_services: list[dict] = doc.get("service", [])  # type: ignore[type-arg]
        services: list[Service] = []
        for i, raw in enumerate(raw_services):
            name = raw.get("name")
            if not name:
                raise ManifestError(
                    f"[[service]] entry #{i} is missing required field 'name'"
                )
            if not isinstance(name, str):
                raise ManifestError(
                    f"[[service]] entry #{i}: name must be a string, got {type(name).__name__}"
                )
            target_str = raw.get("target")
            if target_str is None:
                raise ManifestError(
                    f"service '{name}' is missing required field 'target'"
                )
            if not isinstance(target_str, str):
                raise ManifestError(
                    f"service '{name}': target must be a quoted string like \"0.1\", got {type(target_str).__name__}"
                )
            target = ManifestReader._parse_target(target_str, name)
            command_raw = raw.get("command", "")
            if not isinstance(command_raw, str):
                raise ManifestError(
                    f"service '{name}': command must be a string, got {type(command_raw).__name__}"
                )
            command: str = command_raw
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
            services.append(Service(name=name, target=target, command=command, log=log_mode))

        # --- [[status.url]] ---
        raw_urls: list[dict] = doc.get("status", {}).get("url", [])  # type: ignore[type-arg]
        status_urls: list[StatusUrl] = []
        for raw_url in raw_urls:
            label = raw_url.get("label", "")
            if not isinstance(label, str):
                raise ManifestError(
                    f"[[status.url]] entry: label must be a string, got {type(label).__name__}"
                )
            url = raw_url.get("url", "")
            status_urls.append(StatusUrl(label=label, url=url))

        # --- [logs] ---
        raw_logs: dict = doc.get("logs", {})  # type: ignore[type-arg]
        _log_int_fields = ("rotate_size_bytes", "max_rotations", "retention_seconds")
        log_kwargs: dict = {}  # type: ignore[type-arg]
        for field_name in _log_int_fields:
            if field_name in raw_logs:
                val = raw_logs[field_name]
                if not isinstance(val, int):
                    raise ManifestError(
                        f"[logs] '{field_name}' must be an integer, got {type(val).__name__}"
                    )
                log_kwargs[field_name] = val
        logs = LogConfig(**log_kwargs)

        return ServiceManifest(
            session_prefix=session_prefix,
            env_file=env_file,
            layout_hook=layout_hook,
            services=tuple(services),
            status_urls=tuple(status_urls),
            logs=logs,
        )
