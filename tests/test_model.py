"""Tests for service_manifest.modules.manifest.model — frozen dataclass construction and equality."""

from dataclasses import FrozenInstanceError

from service_manifest.modules.manifest.model import (
    Health,
    HealthType,
    LogConfig,
    LogMode,
    Service,
    ServiceManifest,
    StartupPolicy,
    StatusUrl,
    Target,
)


def test_target_construction_and_equality() -> None:
    t1 = Target(window=0, pane=0)
    t2 = Target(window=0, pane=0)
    assert t1 == t2
    assert t1.window == 0
    assert t1.pane == 0


def test_target_is_frozen() -> None:
    import pytest

    t = Target(window=1, pane=2)
    with pytest.raises(FrozenInstanceError):
        t.window = 99  # type: ignore[misc]


def test_service_construction_and_equality() -> None:
    svc = Service(name="backend", target=Target(window=0, pane=0), command="npm run start:dev")
    assert svc.name == "backend"
    assert svc.target == Target(window=0, pane=0)
    assert svc.command == "npm run start:dev"
    assert svc == Service(name="backend", target=Target(window=0, pane=0), command="npm run start:dev")


def test_service_empty_command_is_legal() -> None:
    """An empty command represents an interactive pane — this must not be rejected."""
    svc = Service(name="shell", target=Target(window=1, pane=0), command="")
    assert svc.command == ""


def test_service_health_is_optional() -> None:
    svc = Service(name="backend", target=Target(window=0, pane=0), command="cmd")
    assert svc.health is None


def test_service_can_have_health_probe() -> None:
    health = Health(type=HealthType.URL, target="http://localhost:${BACKEND_PORT}/health", timeout=2)
    svc = Service(name="backend", target=Target(window=0, pane=0), command="cmd", health=health)
    assert svc.health == health


def test_service_is_frozen() -> None:
    import pytest

    svc = Service(name="x", target=Target(0, 0), command="cmd")
    with pytest.raises(FrozenInstanceError):
        svc.name = "y"  # type: ignore[misc]


def test_status_url_construction_and_equality() -> None:
    su = StatusUrl(label="Backend", url="http://localhost:${BACKEND_PORT}")
    assert su.label == "Backend"
    assert su.url == "http://localhost:${BACKEND_PORT}"
    assert su == StatusUrl(label="Backend", url="http://localhost:${BACKEND_PORT}")


def test_status_url_is_frozen() -> None:
    import pytest

    su = StatusUrl(label="A", url="http://example.com")
    with pytest.raises(FrozenInstanceError):
        su.label = "B"  # type: ignore[misc]


def test_service_manifest_construction() -> None:
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=".winter.env",
        layout_hook="layout-hook.sh",
        services=(
            Service(name="backend", target=Target(0, 0), command="npm run start:dev"),
            Service(name="frontend", target=Target(0, 1), command="npm run dev"),
            Service(name="shell", target=Target(1, 0), command=""),
        ),
        status_urls=(StatusUrl(label="Backend", url="http://localhost:${BACKEND_PORT}"),),
    )
    assert manifest.session_prefix == "mp"
    assert manifest.env_file == ".winter.env"
    assert manifest.layout_hook == "layout-hook.sh"
    assert len(manifest.services) == 3
    assert len(manifest.status_urls) == 1


def test_service_manifest_optional_fields_none() -> None:
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(),
        status_urls=(),
    )
    assert manifest.env_file is None
    assert manifest.layout_hook is None
    assert manifest.services == ()
    assert manifest.status_urls == ()


def test_service_manifest_is_frozen() -> None:
    import pytest

    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(),
        status_urls=(),
    )
    with pytest.raises(FrozenInstanceError):
        manifest.session_prefix = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LogConfig
# ---------------------------------------------------------------------------


def test_log_config_defaults() -> None:
    lc = LogConfig()
    assert lc.rotate_size_bytes == 10485760
    assert lc.max_rotations == 5
    assert lc.retention_seconds == 604800


def test_log_config_custom_values() -> None:
    lc = LogConfig(rotate_size_bytes=1024, max_rotations=3, retention_seconds=86400)
    assert lc.rotate_size_bytes == 1024
    assert lc.max_rotations == 3
    assert lc.retention_seconds == 86400


def test_log_config_is_frozen() -> None:
    import pytest

    lc = LogConfig()
    with pytest.raises(FrozenInstanceError):
        lc.rotate_size_bytes = 999  # type: ignore[misc]


def test_log_config_equality() -> None:
    assert LogConfig() == LogConfig()
    assert LogConfig(max_rotations=0) != LogConfig()


# ---------------------------------------------------------------------------
# ServiceManifest.logs default
# ---------------------------------------------------------------------------


def test_service_manifest_logs_defaults_to_log_config() -> None:
    """ServiceManifest.logs is always present — no null-guard needed."""
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(),
        status_urls=(),
    )
    assert isinstance(manifest.logs, LogConfig)
    assert manifest.logs == LogConfig()


def test_service_manifest_logs_custom() -> None:
    lc = LogConfig(rotate_size_bytes=2048, max_rotations=2, retention_seconds=3600)
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(),
        status_urls=(),
        logs=lc,
    )
    assert manifest.logs == lc


# ---------------------------------------------------------------------------
# Service.log default
# ---------------------------------------------------------------------------


def test_service_log_defaults_to_file_mode() -> None:
    svc = Service(name="backend", target=Target(window=0, pane=0), command="npm start")
    assert svc.log == LogMode.FILE


def test_service_log_can_be_pane_mode() -> None:
    svc = Service(name="shell", target=Target(window=1, pane=0), command="", log=LogMode.PANE)
    assert svc.log == LogMode.PANE


def test_service_log_can_be_memory_mode() -> None:
    svc = Service(name="svc", target=Target(window=0, pane=0), command="cmd", log=LogMode.MEMORY)
    assert svc.log == LogMode.MEMORY


def test_service_manifest_equality() -> None:
    def make() -> ServiceManifest:
        return ServiceManifest(
            session_prefix="mp",
            env_file=".winter.env",
            layout_hook=None,
            services=(Service(name="backend", target=Target(0, 0), command="cmd"),),
            status_urls=(),
        )

    assert make() == make()


# ---------------------------------------------------------------------------
# workspace_services / workspace_layout_hook defaults
# ---------------------------------------------------------------------------


def test_service_manifest_workspace_fields_default() -> None:
    """New workspace fields default to () and None without breaking existing construction."""
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook="layout-hook.sh",
        services=(Service(name="backend", target=Target(0, 0), command="cmd"),),
        status_urls=(StatusUrl(label="BE", url="http://localhost:4020"),),
    )
    assert manifest.workspace_services == ()
    assert manifest.workspace_layout_hook is None


def test_service_manifest_workspace_fields_explicit() -> None:
    ws_svc = Service(name="docker", target=Target(0, 0), command="docker compose up")
    manifest = ServiceManifest(
        session_prefix="mp",
        env_file=None,
        layout_hook=None,
        services=(),
        status_urls=(),
        workspace_services=(ws_svc,),
        workspace_layout_hook="ai/project/workspace-layout-hook.sh",
    )
    assert manifest.workspace_services == (ws_svc,)
    assert manifest.workspace_layout_hook == "ai/project/workspace-layout-hook.sh"


# ---------------------------------------------------------------------------
# StartupPolicy
# ---------------------------------------------------------------------------


def test_startup_policy_defaults() -> None:
    """StartupPolicy has retries=0 and retry_delay=2.0 as defaults."""
    policy = StartupPolicy()
    assert policy.retries == 0
    assert policy.retry_delay == 2.0


def test_startup_policy_custom_values() -> None:
    policy = StartupPolicy(retries=3, retry_delay=5.0)
    assert policy.retries == 3
    assert policy.retry_delay == 5.0


def test_startup_policy_is_frozen() -> None:
    import pytest

    policy = StartupPolicy(retries=1, retry_delay=1.0)
    with pytest.raises(FrozenInstanceError):
        policy.retries = 99  # type: ignore[misc]


def test_service_startup_defaults_to_none() -> None:
    svc = Service(name="backend", target=Target(window=0, pane=0), command="cmd")
    assert svc.startup is None


def test_service_can_have_startup_policy() -> None:
    policy = StartupPolicy(retries=3, retry_delay=2.0)
    svc = Service(name="backend", target=Target(window=0, pane=0), command="cmd", startup=policy)
    assert svc.startup == policy
