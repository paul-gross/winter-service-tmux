"""Semantic validator for a parsed ``ServiceManifest``.

Read-vs-validate split:
- The **reader** raises ``ManifestError`` on structural impossibility — unreadable
  files, malformed TOML, missing required keys.  It cannot produce a
  ``ServiceManifest`` if the data is structurally broken.
- The **validator** operates on an already-parsed ``ServiceManifest`` and
  collects *semantic* issues — duplicates, bad values, unresolvable variables.
  Violations are returned as a list of human-readable strings so callers (the
  doctor probe, a future orchestrator) decide whether they are fatal.

Returning a list instead of raising is intentional: a list of violations is
DATA (the caller decides fatality), not control flow.  See
``winter-harness:/architecture/error-handling.md`` — "when bool/data is honest".
"""

from __future__ import annotations

from service_manifest.modules.manifest.env import interpolate, referenced_vars
from service_manifest.modules.manifest.model import Service, ServiceManifest


class ManifestValidator:
    """Validates a ``ServiceManifest`` for semantic correctness.

    All checks operate on an already-parsed manifest; no file I/O is performed.
    The caller resolves the env file (via ``EnvFileReader``) and passes
    the resulting dict as *env*.  Passing ``env=None`` signals that the env file
    was absent or unavailable; ``${VAR}`` resolvability checks are skipped in
    that case (mirrors the bash ``up`` script's warn-and-continue behaviour for
    a missing env file).  All other checks run regardless.

    Usage::

        validator = ManifestValidator()
        violations = validator.validate(manifest, env={"BACKEND_PORT": "3000"})
        if violations:
            for v in violations:
                print(v)
    """

    def validate(
        self,
        manifest: ServiceManifest,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Check *manifest* for semantic violations and return them as strings.

        Returns an empty list when the manifest is clean.  Each entry is a
        human-readable string that names the offending element.

        Checks performed:
        - ``session_prefix`` is present and non-empty (defensive — reader
          already requires it, but validate defensively here too).
        - Every service ``name`` is non-empty (both env services and workspace
          services).
        - No duplicate service names (checked GLOBALLY across the merged set of
          env + workspace services — the unified ``[[service]]`` config gives
          both scopes one name namespace, so the same name in two scopes is a
          violation).
        - No duplicate ``target`` values across services (checked per-list —
          an env service and a workspace service sharing target "0.0" is LEGAL
          because they live in different tmux sessions).
        - All targets have non-negative ``window`` and ``pane`` values (both
          lists).
        - ``logs.rotate_size_bytes`` is positive (> 0).
        - ``logs.max_rotations`` is non-negative (>= 0).
        - ``logs.retention_seconds`` is non-negative (>= 0).
        - Each service's ``startup.retries`` is non-negative (both lists).
        - Each service's ``startup.retry_delay`` is non-negative (both lists).
        - A ``startup`` policy with ``retries > 0`` is not declared on an
          interactive (empty-command) service, where it would never fire.
        - When *env* is provided: every ``${VAR}`` in a ``status.url`` template
          resolves against *env*; unresolvable vars are reported per-label.
        """
        violations: list[str] = []

        self._check_session_prefix(manifest, violations)
        self._check_service_names(manifest, violations)
        self._check_duplicate_targets(manifest.services, "service", violations)
        self._check_duplicate_targets(manifest.workspace_services, "workspace service", violations)
        self._check_target_non_negative(manifest.services, "service", violations)
        self._check_target_non_negative(manifest.workspace_services, "workspace service", violations)
        self._check_health_config(manifest.services, "service", violations)
        self._check_health_config(manifest.workspace_services, "workspace service", violations)
        self._check_workspace_health_has_no_vars(manifest.workspace_services, violations)
        self._check_startup_config(manifest.services, "service", violations)
        self._check_startup_config(manifest.workspace_services, "workspace service", violations)
        self._check_log_config(manifest, violations)

        if env is not None:
            self._check_status_url_vars(manifest, env, violations)
            self._check_health_vars(manifest.services, "service", env, violations)
            self._check_health_vars(manifest.workspace_services, "workspace service", env, violations)

        return violations

    # ------------------------------------------------------------------
    # Internal checks — each appends to violations, never raises
    # ------------------------------------------------------------------

    @staticmethod
    def _check_session_prefix(manifest: ServiceManifest, violations: list[str]) -> None:
        if not manifest.session_prefix:
            violations.append("'session_prefix' is missing or empty")

    @staticmethod
    def _check_service_names(manifest: ServiceManifest, violations: list[str]) -> None:
        """Check service names across BOTH scopes as one namespace.

        Empty/blank names are reported per-scope (the label distinguishes env
        from workspace services).  Duplicate names are detected GLOBALLY across
        the merged set — the unified ``[[service]]`` config means a name reused
        between the project and workspace scopes is a collision, not two
        independent namespaces.
        """
        seen: set[str] = set()
        scoped: tuple[tuple[Service, str], ...] = (
            *((s, "service") for s in manifest.services),
            *((s, "workspace service") for s in manifest.workspace_services),
        )
        for service, label in scoped:
            if not service.name or not service.name.strip():
                violations.append(
                    f"{label} with target '{service.target.window}.{service.target.pane}' has an empty or blank name"
                )
                continue
            if service.name in seen:
                violations.append(f"duplicate service name '{service.name}'")
            else:
                seen.add(service.name)

    @staticmethod
    def _check_duplicate_targets(services: tuple[Service, ...], label: str, violations: list[str]) -> None:
        target_to_names: dict[tuple[int, int], list[str]] = {}
        for service in services:
            key = (service.target.window, service.target.pane)
            if key not in target_to_names:
                target_to_names[key] = []
            target_to_names[key].append(service.name)

        for (window, pane), names in target_to_names.items():
            if len(names) > 1:
                named = ", ".join(f"'{n}'" for n in names)
                violations.append(f"duplicate target '{window}.{pane}' used by {label}s: {named}")

    @staticmethod
    def _check_target_non_negative(services: tuple[Service, ...], label: str, violations: list[str]) -> None:
        for service in services:
            target = service.target
            if target.window < 0:
                violations.append(f"{label} '{service.name}': target window {target.window} is negative")
            if target.pane < 0:
                violations.append(f"{label} '{service.name}': target pane {target.pane} is negative")

    @staticmethod
    def _check_log_config(manifest: ServiceManifest, violations: list[str]) -> None:
        logs = manifest.logs
        if logs.rotate_size_bytes <= 0:
            violations.append(f"[logs] 'rotate_size_bytes' must be positive, got {logs.rotate_size_bytes}")
        if logs.max_rotations < 0:
            violations.append(f"[logs] 'max_rotations' must be non-negative, got {logs.max_rotations}")
        if logs.retention_seconds < 0:
            violations.append(f"[logs] 'retention_seconds' must be non-negative, got {logs.retention_seconds}")

    @staticmethod
    def _check_health_config(services: tuple[Service, ...], label: str, violations: list[str]) -> None:
        for service in services:
            health = service.health
            if health is None:
                continue
            if not health.target.strip():
                violations.append(f"{label} '{service.name}': health.target must be non-empty")
            if health.timeout is not None and health.timeout <= 0:
                violations.append(f"{label} '{service.name}': health.timeout must be positive, got {health.timeout:g}")

    @staticmethod
    def _check_startup_config(services: tuple[Service, ...], label: str, violations: list[str]) -> None:
        for service in services:
            startup = service.startup
            if startup is None:
                continue
            if startup.retries < 0:
                violations.append(
                    f"{label} '{service.name}': startup.retries must be non-negative, got {startup.retries}"
                )
            if startup.retry_delay < 0:
                violations.append(
                    f"{label} '{service.name}': startup.retry_delay must be non-negative, got {startup.retry_delay:g}"
                )
            if startup.retries > 0 and not service.command.strip():
                violations.append(
                    f"{label} '{service.name}': startup retry policy has no effect on an "
                    "interactive (empty-command) service"
                )

    @staticmethod
    def _check_workspace_health_has_no_vars(services: tuple[Service, ...], violations: list[str]) -> None:
        for service in services:
            if service.health is None:
                continue
            for var in referenced_vars(service.health.target):
                violations.append(
                    f"workspace service '{service.name}' health: variable '${{{var}}}' is not supported "
                    "because workspace services do not load an env_file"
                )

    @staticmethod
    def _check_status_url_vars(
        manifest: ServiceManifest,
        env: dict[str, str],
        violations: list[str],
    ) -> None:
        for status_url in manifest.status_urls:
            _rendered, unresolved = interpolate(status_url.url, env)
            for var in unresolved:
                violations.append(f"status url '{status_url.label}': unresolvable variable '${{{var}}}'")

    @staticmethod
    def _check_health_vars(
        services: tuple[Service, ...],
        label: str,
        env: dict[str, str],
        violations: list[str],
    ) -> None:
        for service in services:
            if service.health is None:
                continue
            _rendered, unresolved = interpolate(service.health.target, env)
            for var in unresolved:
                violations.append(f"{label} '{service.name}' health: unresolvable variable '${{{var}}}'")
