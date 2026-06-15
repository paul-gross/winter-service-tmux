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

from service_manifest.modules.manifest.env import interpolate
from service_manifest.modules.manifest.model import ServiceManifest


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
        - Every service ``name`` is non-empty.
        - No duplicate service names.
        - No duplicate ``target`` values across services.
        - All targets have non-negative ``window`` and ``pane`` values.
        - When *env* is provided: every ``${VAR}`` in a ``status.url`` template
          resolves against *env*; unresolvable vars are reported per-label.
        """
        violations: list[str] = []

        self._check_session_prefix(manifest, violations)
        self._check_service_names(manifest, violations)
        self._check_duplicate_targets(manifest, violations)
        self._check_target_non_negative(manifest, violations)

        if env is not None:
            self._check_status_url_vars(manifest, env, violations)

        return violations

    # ------------------------------------------------------------------
    # Internal checks — each appends to violations, never raises
    # ------------------------------------------------------------------

    @staticmethod
    def _check_session_prefix(
        manifest: ServiceManifest, violations: list[str]
    ) -> None:
        if not manifest.session_prefix:
            violations.append("'session_prefix' is missing or empty")

    @staticmethod
    def _check_service_names(
        manifest: ServiceManifest, violations: list[str]
    ) -> None:
        seen: set[str] = set()
        for service in manifest.services:
            if not service.name or not service.name.strip():
                violations.append(
                    f"service with target '{service.target.window}.{service.target.pane}'"
                    f" has an empty or blank name"
                )
                continue
            if service.name in seen:
                violations.append(
                    f"duplicate service name '{service.name}'"
                )
            else:
                seen.add(service.name)

    @staticmethod
    def _check_duplicate_targets(
        manifest: ServiceManifest, violations: list[str]
    ) -> None:
        target_to_names: dict[tuple[int, int], list[str]] = {}
        for service in manifest.services:
            key = (service.target.window, service.target.pane)
            if key not in target_to_names:
                target_to_names[key] = []
            target_to_names[key].append(service.name)

        for (window, pane), names in target_to_names.items():
            if len(names) > 1:
                named = ", ".join(f"'{n}'" for n in names)
                violations.append(
                    f"duplicate target '{window}.{pane}' used by services: {named}"
                )

    @staticmethod
    def _check_target_non_negative(
        manifest: ServiceManifest, violations: list[str]
    ) -> None:
        for service in manifest.services:
            target = service.target
            if target.window < 0:
                violations.append(
                    f"service '{service.name}': target window {target.window} is negative"
                )
            if target.pane < 0:
                violations.append(
                    f"service '{service.name}': target pane {target.pane} is negative"
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
                violations.append(
                    f"status url '{status_url.label}': unresolvable variable '${{{var}}}'"
                )
