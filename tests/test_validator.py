"""Tests for service_manifest.modules.manifest.validator — semantic checks over ServiceManifest."""

from service_manifest.modules.manifest.model import LogConfig, Service, ServiceManifest, StatusUrl, Target
from service_manifest.modules.manifest.validator import ManifestValidator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    session_prefix: str = "mp",
    env_file: str | None = None,
    layout_hook: str | None = None,
    services: tuple[Service, ...] = (),
    status_urls: tuple[StatusUrl, ...] = (),
    logs: LogConfig = LogConfig(),
) -> ServiceManifest:
    return ServiceManifest(
        session_prefix=session_prefix,
        env_file=env_file,
        layout_hook=layout_hook,
        services=services,
        status_urls=status_urls,
        logs=logs,
    )


def _service(name: str, window: int = 0, pane: int = 0, command: str = "cmd") -> Service:
    return Service(name=name, target=Target(window=window, pane=pane), command=command)


def _status_url(label: str, url: str) -> StatusUrl:
    return StatusUrl(label=label, url=url)


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
        status_urls=(_status_url("Backend", "http://localhost:${BACKEND_PORT}"),),
    )
    env = {"BACKEND_PORT": "3000"}
    assert _validator.validate(manifest, env=env) == []


def test_valid_manifest_no_env_no_status_urls() -> None:
    manifest = _make_manifest(
        session_prefix="proj",
        services=(_service("worker", window=0, pane=0),),
    )
    assert _validator.validate(manifest) == []


def test_valid_manifest_env_none_skips_var_check() -> None:
    """env=None: ${VAR} checks skipped; other checks still run."""
    manifest = _make_manifest(
        session_prefix="mp",
        status_urls=(_status_url("Backend", "http://localhost:${MISSING_VAR}"),),
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
        services=(Service(name="   ", target=Target(0, 0), command="cmd"),),
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
        services=(Service(name="svc", target=Target(-1, 0), command="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert any("window" in v and "-1" in v for v in violations)


def test_negative_pane_is_violation() -> None:
    manifest = _make_manifest(
        services=(Service(name="svc", target=Target(0, -1), command="cmd"),),
    )
    violations = _validator.validate(manifest)
    assert any("pane" in v and "-1" in v for v in violations)


def test_zero_window_and_pane_no_violation() -> None:
    manifest = _make_manifest(
        services=(Service(name="svc", target=Target(0, 0), command="cmd"),),
    )
    assert _validator.validate(manifest) == []


# ---------------------------------------------------------------------------
# ${VAR} resolvability checks
# ---------------------------------------------------------------------------


def test_unresolvable_var_in_status_url_with_env_is_violation() -> None:
    manifest = _make_manifest(
        status_urls=(_status_url("Backend", "http://localhost:${BACKEND_PORT}"),),
    )
    # env provided but BACKEND_PORT is absent
    violations = _validator.validate(manifest, env={})
    assert len(violations) == 1
    v = violations[0]
    assert "Backend" in v
    assert "BACKEND_PORT" in v


def test_resolvable_var_in_status_url_with_env_no_violation() -> None:
    manifest = _make_manifest(
        status_urls=(_status_url("Backend", "http://localhost:${BACKEND_PORT}"),),
    )
    violations = _validator.validate(manifest, env={"BACKEND_PORT": "3000"})
    assert violations == []


def test_unresolvable_var_skipped_when_env_is_none() -> None:
    """env=None: ${VAR} resolvability check must be skipped entirely."""
    manifest = _make_manifest(
        status_urls=(_status_url("Backend", "http://localhost:${BACKEND_PORT}"),),
    )
    assert _validator.validate(manifest, env=None) == []


def test_unresolvable_var_names_status_label_and_var() -> None:
    manifest = _make_manifest(
        status_urls=(_status_url("Frontend", "http://localhost:${FRONTEND_PORT}/app"),),
    )
    violations = _validator.validate(manifest, env={})
    assert len(violations) == 1
    assert "Frontend" in violations[0]
    assert "FRONTEND_PORT" in violations[0]


# ---------------------------------------------------------------------------
# Multiple simultaneous violations
# ---------------------------------------------------------------------------


def test_multiple_violations_all_reported() -> None:
    """Duplicate name + duplicate target + negative window + unresolvable var."""
    manifest = _make_manifest(
        session_prefix="mp",
        services=(
            # negative window
            Service(name="svc-a", target=Target(-1, 0), command="cmd"),
            # duplicate name with svc-a
            Service(name="svc-a", target=Target(0, 1), command="cmd"),
            # duplicate target with next service
            Service(name="svc-b", target=Target(1, 0), command="cmd"),
            Service(name="svc-c", target=Target(1, 0), command="cmd"),
        ),
        status_urls=(_status_url("Api", "http://localhost:${API_PORT}"),),
    )
    violations = _validator.validate(manifest, env={})

    # negative window on svc-a
    assert any("svc-a" in v and "window" in v for v in violations)
    # duplicate name svc-a
    assert any("duplicate service name" in v and "svc-a" in v for v in violations)
    # duplicate target 1.0 for svc-b and svc-c
    assert any("1.0" in v and "svc-b" in v and "svc-c" in v for v in violations)
    # unresolvable ${API_PORT}
    assert any("API_PORT" in v for v in violations)

    # Ensure more than one violation
    assert len(violations) >= 4


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
