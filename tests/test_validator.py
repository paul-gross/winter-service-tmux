"""Tests for service_manifest.modules.manifest.validator — semantic checks over ServiceManifest."""

from service_manifest.modules.manifest.model import (
    Health,
    HealthType,
    LogConfig,
    Service,
    ServiceManifest,
    StartupPolicy,
    Target,
)
from service_manifest.modules.manifest.validator import ManifestValidator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    session_prefix: str = "mp",
    env_file: str | None = None,
    layout_hook: str | None = None,
    services: tuple[Service, ...] = (),
    logs: LogConfig = LogConfig(),
    workspace_services: tuple[Service, ...] = (),
    workspace_layout_hook: str | None = None,
) -> ServiceManifest:
    return ServiceManifest(
        session_prefix=session_prefix,
        env_file=env_file,
        layout_hook=layout_hook,
        services=services,
        logs=logs,
        workspace_services=workspace_services,
        workspace_layout_hook=workspace_layout_hook,
    )


def _service(name: str, window: int = 0, pane: int = 0, command: str = "cmd") -> Service:
    return Service(name=name, target=Target(window=window, pane=pane), cmd=command)


def _service_with_health(name: str, health: Health, window: int = 0, pane: int = 0) -> Service:
    return Service(name=name, target=Target(window=window, pane=pane), cmd="cmd", health=health)


_validator = ManifestValidator()


# ---------------------------------------------------------------------------
# Valid manifest → empty violations
# ---------------------------------------------------------------------------


def test_valid_manifest_returns_empty_list() -> None:
    manifest = _make_manifest(
        session_prefix="mp",
        services=(
            _service("backend", window=0, pane=0),
            _service("frontend", window=0, pane=1),
        ),
    )
    assert _validator.validate(manifest, env={"BACKEND_PORT": "3000"}) == []


def test_valid_manifest_no_services() -> None:
    manifest = _make_manifest(
        session_prefix="proj",
        services=(_service("worker", window=0, pane=0),),
    )
    assert _validator.validate(manifest) == []


def test_valid_manifest_env_none_skips_health_var_check() -> None:
    """env=None: ${VAR} checks skipped; other checks still run."""
    manifest = _make_manifest(
        session_prefix="mp",
        services=(
            _service_with_health(
                "backend",
                Health(type=HealthType.URL, target="http://localhost:${MISSING_VAR}/health"),
            ),
        ),
    )
    # No env provided — var check skipped, so no violation
    assert _validator.validate(manifest, env=None) == []


# ---------------------------------------------------------------------------
# session_prefix checks
# ---------------------------------------------------------------------------


def test_empty_session_prefix_is_violation() -> None:
    manifest = _make_manifest(session_prefix="")
    violations = _validator.validate(manifest)
    assert len(violations) == 1
    assert "session_prefix" in violations[0]


# ---------------------------------------------------------------------------
# Service name checks
# ---------------------------------------------------------------------------


def test_empty_service_name_is_violation() -> None:
    manifest = _make_manifest(
        services=(_service("", window=0, pane=0),),
    )
    violations = _validator.validate(manifest)
    assert len(violations) == 1
    assert "empty" in violations[0] or "blank" in violations[0]


def test_blank_service_name_is_violation() -> None:
    manifest = _make_manifest(
        services=(Service(name="   ", target=Target(0, 0), cmd="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert len(violations) == 1
    assert "empty" in violations[0] or "blank" in violations[0]


def test_duplicate_service_name_is_violation() -> None:
    manifest = _make_manifest(
        services=(
            _service("backend", window=0, pane=0),
            _service("backend", window=0, pane=1),
        ),
    )
    violations = _validator.validate(manifest)
    assert any("duplicate service name" in v and "backend" in v for v in violations)


def test_unique_service_names_no_violation() -> None:
    manifest = _make_manifest(
        services=(
            _service("backend", window=0, pane=0),
            _service("frontend", window=0, pane=1),
        ),
    )
    assert _validator.validate(manifest) == []


# ---------------------------------------------------------------------------
# Duplicate target checks
# ---------------------------------------------------------------------------


def test_duplicate_target_is_violation_naming_both_services() -> None:
    manifest = _make_manifest(
        services=(
            _service("alpha", window=0, pane=0),
            _service("beta", window=0, pane=0),
        ),
    )
    violations = _validator.validate(manifest)
    assert len(violations) == 1
    v = violations[0]
    assert "0.0" in v
    assert "alpha" in v
    assert "beta" in v


def test_unique_targets_no_violation() -> None:
    manifest = _make_manifest(
        services=(
            _service("alpha", window=0, pane=0),
            _service("beta", window=0, pane=1),
            _service("gamma", window=1, pane=0),
        ),
    )
    assert _validator.validate(manifest) == []


# ---------------------------------------------------------------------------
# Non-negative target checks
# ---------------------------------------------------------------------------


def test_negative_window_is_violation() -> None:
    manifest = _make_manifest(
        services=(Service(name="svc", target=Target(-1, 0), cmd="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert any("window" in v and "-1" in v for v in violations)


def test_negative_pane_is_violation() -> None:
    manifest = _make_manifest(
        services=(Service(name="svc", target=Target(0, -1), cmd="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert any("pane" in v and "-1" in v for v in violations)


def test_zero_window_and_pane_no_violation() -> None:
    manifest = _make_manifest(
        services=(Service(name="svc", target=Target(0, 0), cmd="cmd"),),
    )
    assert _validator.validate(manifest) == []


# ---------------------------------------------------------------------------
# ${VAR} resolvability checks — health checks
# ---------------------------------------------------------------------------


def test_unresolvable_var_in_service_health_with_env_is_violation() -> None:
    manifest = _make_manifest(
        services=(
            _service_with_health(
                "backend",
                Health(type=HealthType.URL, target="http://localhost:${BACKEND_PORT}/health"),
            ),
        ),
    )
    violations = _validator.validate(manifest, env={})

    assert len(violations) == 1
    assert "backend" in violations[0]
    assert "BACKEND_PORT" in violations[0]


def test_resolvable_var_in_service_health_no_violation() -> None:
    manifest = _make_manifest(
        services=(
            _service_with_health(
                "backend",
                Health(type=HealthType.URL, target="http://localhost:${BACKEND_PORT}/health"),
            ),
        ),
    )

    assert _validator.validate(manifest, env={"BACKEND_PORT": "3000"}) == []


def test_service_health_blank_target_is_violation() -> None:
    manifest = _make_manifest(services=(_service_with_health("backend", Health(type=HealthType.CMD, target=" ")),))
    violations = _validator.validate(manifest)

    assert any("health.target" in v for v in violations)


def test_service_health_non_positive_timeout_is_violation() -> None:
    manifest = _make_manifest(
        services=(_service_with_health("backend", Health(type=HealthType.CMD, target="true", timeout=0)),)
    )
    violations = _validator.validate(manifest)

    assert any("health.timeout" in v for v in violations)


def test_workspace_service_health_var_is_violation() -> None:
    manifest = _make_manifest(
        workspace_services=(
            _service_with_health(
                "docker",
                Health(type=HealthType.URL, target="http://localhost:${DOCKER_PORT}/health"),
            ),
        ),
    )
    violations = _validator.validate(manifest, env={"DOCKER_PORT": "5000"})

    assert len(violations) == 1
    assert "workspace service 'docker' health" in violations[0]
    assert "DOCKER_PORT" in violations[0]


# ---------------------------------------------------------------------------
# Multiple simultaneous violations
# ---------------------------------------------------------------------------


def test_multiple_violations_all_reported() -> None:
    """Duplicate name + duplicate target + negative window."""
    manifest = _make_manifest(
        session_prefix="mp",
        services=(
            # negative window
            Service(name="svc-a", target=Target(-1, 0), cmd="cmd"),
            # duplicate name with svc-a
            Service(name="svc-a", target=Target(0, 1), cmd="cmd"),
            # duplicate target with next service
            Service(name="svc-b", target=Target(1, 0), cmd="cmd"),
            Service(name="svc-c", target=Target(1, 0), cmd="cmd"),
        ),
    )
    violations = _validator.validate(manifest, env={})

    # negative window on svc-a
    assert any("svc-a" in v and "window" in v for v in violations)
    # duplicate name svc-a
    assert any("duplicate service name" in v and "svc-a" in v for v in violations)
    # duplicate target 1.0 for svc-b and svc-c
    assert any("1.0" in v and "svc-b" in v and "svc-c" in v for v in violations)

    # Ensure more than one violation
    assert len(violations) >= 3


# ---------------------------------------------------------------------------
# [logs] value checks
# ---------------------------------------------------------------------------


def test_valid_log_config_no_violations() -> None:
    manifest = _make_manifest(logs=LogConfig(rotate_size_bytes=1024, max_rotations=5, retention_seconds=3600))
    assert _validator.validate(manifest) == []


def test_log_config_defaults_no_violations() -> None:
    """Default LogConfig values must all pass validation."""
    manifest = _make_manifest()
    assert _validator.validate(manifest) == []


def test_rotate_size_bytes_zero_is_violation() -> None:
    manifest = _make_manifest(logs=LogConfig(rotate_size_bytes=0))
    violations = _validator.validate(manifest)
    assert any("rotate_size_bytes" in v for v in violations)


def test_rotate_size_bytes_negative_is_violation() -> None:
    manifest = _make_manifest(logs=LogConfig(rotate_size_bytes=-1))
    violations = _validator.validate(manifest)
    assert any("rotate_size_bytes" in v and "-1" in v for v in violations)


def test_max_rotations_negative_is_violation() -> None:
    manifest = _make_manifest(logs=LogConfig(max_rotations=-1))
    violations = _validator.validate(manifest)
    assert any("max_rotations" in v and "-1" in v for v in violations)


def test_max_rotations_zero_is_valid() -> None:
    """Zero disables rotation — it is explicitly allowed."""
    manifest = _make_manifest(logs=LogConfig(max_rotations=0))
    assert _validator.validate(manifest) == []


def test_retention_seconds_negative_is_violation() -> None:
    manifest = _make_manifest(logs=LogConfig(retention_seconds=-1))
    violations = _validator.validate(manifest)
    assert any("retention_seconds" in v and "-1" in v for v in violations)


def test_retention_seconds_zero_is_valid() -> None:
    """Zero disables time-based pruning — it is explicitly allowed."""
    manifest = _make_manifest(logs=LogConfig(retention_seconds=0))
    assert _validator.validate(manifest) == []


def test_multiple_log_violations_all_reported() -> None:
    manifest = _make_manifest(logs=LogConfig(rotate_size_bytes=0, max_rotations=-2, retention_seconds=-3))
    violations = _validator.validate(manifest)
    assert any("rotate_size_bytes" in v for v in violations)
    assert any("max_rotations" in v for v in violations)
    assert any("retention_seconds" in v for v in violations)
    assert len(violations) == 3


# ---------------------------------------------------------------------------
# workspace_services — semantic checks
# ---------------------------------------------------------------------------


def test_valid_workspace_services_no_violations() -> None:
    manifest = _make_manifest(
        workspace_services=(
            _service("docker", window=0, pane=0),
            _service("monitor", window=0, pane=1),
        ),
    )
    assert _validator.validate(manifest) == []


def test_empty_workspace_service_name_is_violation() -> None:
    manifest = _make_manifest(
        workspace_services=(Service(name="", target=Target(0, 0), cmd="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert len(violations) == 1
    assert "workspace service" in violations[0]
    assert "empty" in violations[0] or "blank" in violations[0]


def test_blank_workspace_service_name_is_violation() -> None:
    manifest = _make_manifest(
        workspace_services=(Service(name="   ", target=Target(0, 0), cmd="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert len(violations) == 1
    assert "workspace service" in violations[0]


def test_duplicate_workspace_service_name_is_violation() -> None:
    manifest = _make_manifest(
        workspace_services=(
            _service("docker", window=0, pane=0),
            _service("docker", window=0, pane=1),
        ),
    )
    violations = _validator.validate(manifest)
    assert any("duplicate service name" in v and "docker" in v for v in violations)


def test_duplicate_workspace_service_target_is_violation() -> None:
    manifest = _make_manifest(
        workspace_services=(
            _service("docker", window=0, pane=0),
            _service("monitor", window=0, pane=0),
        ),
    )
    violations = _validator.validate(manifest)
    assert len(violations) == 1
    v = violations[0]
    assert "0.0" in v
    assert "docker" in v
    assert "monitor" in v
    assert "workspace service" in v


def test_negative_window_in_workspace_service_is_violation() -> None:
    manifest = _make_manifest(
        workspace_services=(Service(name="docker", target=Target(-1, 0), cmd="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert any("workspace service" in v and "window" in v and "-1" in v for v in violations)


def test_negative_pane_in_workspace_service_is_violation() -> None:
    manifest = _make_manifest(
        workspace_services=(Service(name="docker", target=Target(0, -1), cmd="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert any("workspace service" in v and "pane" in v and "-1" in v for v in violations)


def test_env_and_workspace_service_sharing_target_is_not_a_violation() -> None:
    """An env service and a workspace service may share target 0.0 — different sessions."""
    manifest = _make_manifest(
        services=(_service("backend", window=0, pane=0),),
        workspace_services=(_service("docker", window=0, pane=0),),
    )
    assert _validator.validate(manifest) == []


def test_duplicate_target_within_env_services_still_violation_when_workspace_present() -> None:
    """Duplicate env service targets are still caught even when workspace_services exist."""
    manifest = _make_manifest(
        services=(
            _service("alpha", window=0, pane=0),
            _service("beta", window=0, pane=0),
        ),
        workspace_services=(_service("docker", window=0, pane=0),),
    )
    violations = _validator.validate(manifest)
    # env services alpha and beta share 0.0 → violation
    assert any("0.0" in v and "alpha" in v and "beta" in v for v in violations)
    # env+workspace sharing 0.0 → no cross-list violation
    assert not any("docker" in v and "alpha" in v for v in violations)
    assert not any("docker" in v and "beta" in v for v in violations)


def test_duplicate_names_within_each_scope_are_violations() -> None:
    """Duplicate names within the project list and within the workspace list both flag."""
    manifest = _make_manifest(
        services=(
            _service("env-dup", window=0, pane=0),
            _service("env-dup", window=0, pane=1),
        ),
        workspace_services=(
            _service("ws-dup", window=1, pane=0),
            _service("ws-dup", window=1, pane=1),
        ),
    )
    violations = _validator.validate(manifest)
    assert any("duplicate service name" in v and "env-dup" in v for v in violations)
    assert any("duplicate service name" in v and "ws-dup" in v for v in violations)


def test_same_name_across_scopes_is_violation() -> None:
    """Names share ONE namespace across project + workspace — a reuse across scopes collides.

    The unified [[service]] config means project and workspace services no longer
    have independent name namespaces (target uniqueness stays per-scope, names do not).
    """
    manifest = _make_manifest(
        services=(_service("shared", window=0, pane=0),),
        workspace_services=(_service("shared", window=0, pane=1),),
    )
    violations = _validator.validate(manifest)
    assert any("duplicate service name" in v and "shared" in v for v in violations)


def test_same_name_across_scopes_with_shared_target_is_name_violation_only() -> None:
    """A project + workspace service sharing both name and target 0.0: name collides,
    but the shared target stays legal (targets are still per-scope)."""
    manifest = _make_manifest(
        services=(_service("shared", window=0, pane=0),),
        workspace_services=(_service("shared", window=0, pane=0),),
    )
    violations = _validator.validate(manifest)
    assert any("duplicate service name" in v and "shared" in v for v in violations)
    # The shared 0.0 target across scopes must NOT be reported as a duplicate target.
    assert not any("duplicate target" in v for v in violations)


# ---------------------------------------------------------------------------
# startup policy checks
# ---------------------------------------------------------------------------


def _service_with_startup(name: str, startup: StartupPolicy, window: int = 0, pane: int = 0) -> Service:
    return Service(name=name, target=Target(window=window, pane=pane), cmd="cmd", startup=startup)


def test_startup_negative_retries_is_violation() -> None:
    manifest = _make_manifest(
        services=(_service_with_startup("backend", StartupPolicy(retries=-1, retry_delay=2.0)),),
    )
    violations = _validator.validate(manifest)
    assert any("startup.retries" in v and "-1" in v for v in violations)


def test_startup_negative_retry_delay_is_violation() -> None:
    manifest = _make_manifest(
        services=(_service_with_startup("backend", StartupPolicy(retries=0, retry_delay=-0.5)),),
    )
    violations = _validator.validate(manifest)
    assert any("startup.retry_delay" in v for v in violations)


def test_startup_valid_policy_no_violation() -> None:
    manifest = _make_manifest(
        services=(_service_with_startup("backend", StartupPolicy(retries=3, retry_delay=2.0)),),
    )
    assert _validator.validate(manifest) == []


def test_startup_zero_retries_and_zero_delay_no_violation() -> None:
    """Zero is the minimum allowed value for both fields."""
    manifest = _make_manifest(
        services=(_service_with_startup("backend", StartupPolicy(retries=0, retry_delay=0.0)),),
    )
    assert _validator.validate(manifest) == []


def test_startup_retries_on_empty_command_service_is_violation() -> None:
    """A retry policy has no effect on an interactive (empty-command) pane."""
    shell = Service(
        name="shell",
        target=Target(window=0, pane=0),
        cmd="",
        startup=StartupPolicy(retries=3, retry_delay=2.0),
    )
    manifest = _make_manifest(services=(shell,))
    violations = _validator.validate(manifest)
    assert any("shell" in v and "interactive (empty-command)" in v for v in violations)


def test_startup_zero_retries_on_empty_command_service_is_clean() -> None:
    """retries=0 (the opt-out default) on an interactive pane is harmless, not flagged."""
    shell = Service(
        name="shell",
        target=Target(window=0, pane=0),
        cmd="",
        startup=StartupPolicy(retries=0, retry_delay=2.0),
    )
    manifest = _make_manifest(services=(shell,))
    assert _validator.validate(manifest) == []
