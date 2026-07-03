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

import posixpath
import re

from service_manifest.modules.manifest.env import interpolate, referenced_vars
from service_manifest.modules.manifest.model import (
    HealthType,
    Service,
    ServiceManifest,
    parse_port_expression,
    parse_uptime_duration,
)


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
        - Each service's ``cwd``, when declared, is a relative path that does
          not escape its scope root (not absolute, and does not normalize to
          ``..`` or start with ``../``).
        - A ``health.type = "log"`` probe is not declared on an interactive
          (empty-command) service, where there is no captured output to match.
        - A ``health.type = "log"`` probe's ``target`` compiles as a valid
          regular expression.
        - A ``health.type = "uptime"`` probe is not declared on an interactive
          (empty-command) service either — there is no measured child process,
          so ``child_uptime_seconds`` is always ``None`` and the probe would
          always report unhealthy.
        - A ``health.type = "uptime"`` probe's ``target`` parses as a valid
          duration (``<N><unit>``, unit one of ``s``/``m``/``h``/``d``).
        - When *env* is provided: every ``${VAR}`` in a service health target
          resolves against *env*; unresolvable vars are reported per-service.
          Skipped for ``health.type = "log"`` — its ``target`` is used
          verbatim, never interpolated.
        - Each service's ``depends_on`` entries are checked PER SCOPE (a
          service's dependents are resolved against its own scope's service
          list only — env services against env services, workspace services
          against workspace services). A bare pattern, or a pattern
          explicitly qualified with this scope's own name (``"workspace/..."``
          when checking workspace services — the workspace scope name is
          statically known, unlike a per-env scope name), is treated as a
          same-scope reference; any other qualified pattern (e.g.
          ``"workspace/db"`` seen while checking env services) is a
          cross-scope reference this validator cannot resolve statically and
          is skipped. A bare (or self-qualified) pattern equal to the
          service's own name is an absolute self-reference. A bare (or
          self-qualified) pattern matching no service in the same scope is
          reported. Any cycle formed by same-scope ``depends_on`` edges is
          reported once, naming the cycle path. A same-scope pattern that
          targets an interactive (empty-command) service with no declared
          health probe is also reported — that target can never report
          ``state = "running"``, so the dependency would time out on every
          ``winter service up``. NOTE: a genuine cross-scope cycle (e.g.
          ``env/A`` depends on ``workspace/B`` which depends on
          ``workspace/A``) is an unsupported topology this validator does not
          detect — see ``Service.depends_on``.
        """
        violations: list[str] = []

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
        self._check_port_config(manifest.services, "service", violations)
        self._check_port_config(manifest.workspace_services, "workspace service", violations)
        self._check_cwd_config(manifest.services, "service", violations)
        self._check_cwd_config(manifest.workspace_services, "workspace service", violations)
        self._check_depends_on(manifest.services, "service", violations)
        self._check_depends_on(manifest.workspace_services, "workspace service", violations, scope_name="workspace")

        if env is not None:
            self._check_health_vars(manifest.services, "service", env, violations)
            self._check_health_vars(manifest.workspace_services, "workspace service", env, violations)

        return violations

    # ------------------------------------------------------------------
    # Internal checks — each appends to violations, never raises
    # ------------------------------------------------------------------

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
            if health.type == HealthType.LOG:
                if not service.cmd.strip():
                    violations.append(
                        f"{label} '{service.name}': a 'log' health probe has no captured output to match on an "
                        "interactive (empty-command) service"
                    )
                elif health.target.strip():
                    try:
                        re.compile(health.target)
                    except re.error as exc:
                        violations.append(f"{label} '{service.name}': health.target is not a valid regex — {exc}")
            if health.type == HealthType.UPTIME:
                if not service.cmd.strip():
                    violations.append(
                        f"{label} '{service.name}': an 'uptime' health probe has no process to measure on an "
                        "interactive (empty-command) service — child_uptime_seconds is always None, so it is "
                        "always unhealthy"
                    )
                elif health.target.strip() and parse_uptime_duration(health.target) is None:
                    violations.append(
                        f"{label} '{service.name}': health.target {health.target!r} is not a valid duration; "
                        "expected <N><unit> where unit is one of s/m/h/d, e.g. '30s', '5m', '1h', '2d'"
                    )

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
            if startup.retries > 0 and not service.cmd.strip():
                violations.append(
                    f"{label} '{service.name}': startup retry policy has no effect on an "
                    "interactive (empty-command) service"
                )

    @staticmethod
    def _check_port_config(services: tuple[Service, ...], label: str, violations: list[str]) -> None:
        for service in services:
            port = service.port
            if port is None:
                continue
            if isinstance(port, int):
                if port <= 0:
                    violations.append(f"{label} '{service.name}': port must be a positive integer, got {port}")
                continue
            # String expression — must match "WINTER_PORT_BASE + <offset>"
            if parse_port_expression(port) is None:
                violations.append(
                    f"{label} '{service.name}': port expression {port!r} is not valid; "
                    "expected a positive integer or 'WINTER_PORT_BASE + <offset>'"
                )

    @staticmethod
    def _check_cwd_config(services: tuple[Service, ...], label: str, violations: list[str]) -> None:
        for service in services:
            cwd = service.cwd
            if cwd is None:
                continue
            if posixpath.isabs(cwd):
                violations.append(f"{label} '{service.name}': cwd must be a relative path, got absolute {cwd!r}")
                continue
            normalized = posixpath.normpath(cwd)
            if normalized == ".." or normalized.startswith("../"):
                violations.append(f"{label} '{service.name}': cwd {cwd!r} normalizes outside its scope root")

    @staticmethod
    def _check_workspace_health_has_no_vars(services: tuple[Service, ...], violations: list[str]) -> None:
        for service in services:
            if service.health is None or service.health.type == HealthType.LOG:
                # 'log' targets are used verbatim (no ${VAR} interpolation), so
                # a regex that happens to contain '${...}' is not a variable ref.
                continue
            for var in referenced_vars(service.health.target):
                violations.append(
                    f"workspace service '{service.name}' health: variable '${{{var}}}' is not supported "
                    "because workspace services do not load an env_file"
                )

    @staticmethod
    def _check_health_vars(
        services: tuple[Service, ...],
        label: str,
        env: dict[str, str],
        violations: list[str],
    ) -> None:
        for service in services:
            if service.health is None or service.health.type == HealthType.LOG:
                # 'log' targets are used verbatim (no ${VAR} interpolation).
                continue
            _rendered, unresolved = interpolate(service.health.target, env)
            for var in unresolved:
                violations.append(f"{label} '{service.name}' health: unresolvable variable '${{{var}}}'")

    @staticmethod
    def _check_depends_on(
        services: tuple[Service, ...],
        label: str,
        violations: list[str],
        scope_name: str | None = None,
    ) -> None:
        """Check ``depends_on`` for self-references, unresolvable local patterns, and cycles.

        A BARE pattern (no ``"/"``) is always checked here — it must resolve
        against another service declared in this SAME *services* list (the
        service's own scope). A pattern containing ``"/"`` is ALSO checked
        here, as a same-scope reference, when its scope segment equals
        *scope_name* (e.g. ``"workspace/db"`` while validating
        ``manifest.workspace_services``, where *scope_name* is the statically
        known ``"workspace"`` token) — that qualified spelling resolves to the
        identical poll target as the bare form, so it must be validated
        identically too. *scope_name* is ``None`` when validating per-env
        services (``manifest.services``): a per-env scope's actual name (the
        feature-env id) is not known at manifest-validation time, so a
        qualified pattern is always treated as an unresolvable cross-scope
        reference there and skipped — trusted to the runtime
        ``winter service status`` seam and excluded from both the
        resolvability check and cycle detection.

        A same-scope pattern that targets an interactive (empty-command)
        service with no declared health probe is reported: that target's
        ``state`` never becomes ``"running"`` (no captured child process),
        health stays ``"unknown"`` (no probe), so the dependency can never be
        satisfied — see ``_dependency_ready``'s health-then-state rule.
        """
        names = {s.name for s in services}
        by_name = {s.name: s for s in services}
        prefix = f"{scope_name}/" if scope_name is not None else None
        graph: dict[str, list[str]] = {}
        for service in services:
            local_deps: list[str] = []
            for dep in service.depends_on:
                if "/" in dep:
                    if prefix is None or not dep.startswith(prefix):
                        continue
                    dep = dep[len(prefix) :]
                if dep == service.name:
                    violations.append(f"{label} '{service.name}': depends_on references itself ('{dep}')")
                    continue
                if dep not in names:
                    violations.append(
                        f"{label} '{service.name}': depends_on pattern '{dep}' matches no service in scope"
                    )
                    continue
                target = by_name[dep]
                if not target.cmd.strip() and target.health is None:
                    violations.append(
                        f"{label} '{service.name}': depends_on '{dep}' targets an interactive "
                        "(empty-command) service with no health probe — it can never report "
                        "state='running', so this dependency will always time out"
                    )
                local_deps.append(dep)
            graph[service.name] = local_deps

        cycle = _find_depends_on_cycle(graph)
        if cycle is not None:
            violations.append(f"depends_on cycle detected: {' -> '.join(cycle)}")


def _find_depends_on_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return the node sequence of the first cycle found in *graph*, or ``None``.

    *graph* maps a service name to the list of same-scope service names it
    depends on. Standard white/gray/black DFS cycle detection; iterates
    ``graph`` in insertion order (declaration order) so the result is
    deterministic.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def _dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for neighbor in graph.get(node, []):
            neighbor_color = color.get(neighbor, WHITE)
            if neighbor_color == GRAY:
                cycle_start = stack.index(neighbor)
                return [*stack[cycle_start:], neighbor]
            if neighbor_color == WHITE:
                found = _dfs(neighbor)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            found = _dfs(node)
            if found is not None:
                return found
    return None
