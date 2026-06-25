"""Unit tests for SelectorService — pattern selection and expansion logic.

Constructs SelectorService(FakeTmuxRepository(...), builder) in isolation
and covers all public methods:
- split_workspace_patterns
- read_manifest_context (including workspace-first seed regression)
- expand_env_patterns (literal / glob / cross-env / multi-pattern / dead-pattern)
- expand_workspace_patterns
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from service_manifest.modules.manifest.model import Service, ServiceManifest, Target
from service_orchestrator.modules.orchestrate.selector_service import SelectorService
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import WORKSPACE_TARGET
from tests.conftest import FakeTmuxRepository

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/fake/workspace")
_CONFIG_DIR = _WORKSPACE / ".winter" / "config" / "winter-service-tmux"

_MANIFEST = ServiceManifest(
    session_prefix="mp",
    env_file=".winter.env",
    layout_hook=None,
    services=(
        Service(name="backend", target=Target(window=0, pane=0), cmd="cmd"),
        Service(name="worker", target=Target(window=0, pane=1), cmd="cmd"),
    ),
    workspace_services=(
        Service(name="ws-backend", target=Target(window=0, pane=0), cmd="ws-cmd"),
        Service(name="ws-worker", target=Target(window=0, pane=1), cmd="ws-cmd"),
    ),
)

_SERVICE_NAMES = [svc.name for svc in _MANIFEST.services]
_WS_SERVICE_NAMES = [svc.name for svc in _MANIFEST.workspace_services]
_PREFIX = _MANIFEST.session_prefix


def _make_ctx(env: str = "alpha") -> SessionContext:
    return SessionContext(
        env=env,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE / env,
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.services,
        layout_hook=_MANIFEST.layout_hook,
        logs=_MANIFEST.logs,
        env_vars=None,
        env_file_path=None,
    )


def _make_workspace_ctx() -> SessionContext:
    return SessionContext(
        env=WORKSPACE_TARGET,
        workspace_root=_WORKSPACE,
        worktree_dir=_WORKSPACE,
        config_dir=_CONFIG_DIR,
        session_prefix=_MANIFEST.session_prefix,
        services=_MANIFEST.workspace_services,
        layout_hook=_MANIFEST.workspace_layout_hook,
        logs=_MANIFEST.logs,
        env_vars=None,
        env_file_path=None,
    )


class _FakeBuilder:
    """Minimal builder fake for SelectorService tests.

    build() returns a context for the named env; build_workspace() returns
    the workspace context.  build_raises can be set to make both raise.
    """

    def __init__(self, build_raises: Exception | None = None) -> None:
        self._build_raises = build_raises
        self.build_calls: list[str] = []
        self.build_workspace_calls: list[Path | None] = []

    def build(self, env: str, *, workspace_root: Path | None = None, skip_env_file: bool = False) -> SessionContext:
        self.build_calls.append(env)
        if self._build_raises is not None:
            raise self._build_raises
        return _make_ctx(env)

    def build_workspace(self, *, workspace_root: Path | None = None) -> SessionContext:
        self.build_workspace_calls.append(workspace_root)
        if self._build_raises is not None:
            raise self._build_raises
        return _make_workspace_ctx()


def _make_tmux(sessions: list[str]) -> FakeTmuxRepository:
    """Return a FakeTmuxRepository with the given session names (no panes needed)."""
    return FakeTmuxRepository(sessions={s: {} for s in sessions})


def _make_selector(sessions: list[str], build_raises: Exception | None = None) -> SelectorService:
    tmux = _make_tmux(sessions)
    builder: Any = _FakeBuilder(build_raises=build_raises)
    return SelectorService(tmux, builder)


# ---------------------------------------------------------------------------
# split_workspace_patterns
# ---------------------------------------------------------------------------


def test_split_workspace_patterns_empty() -> None:
    sel = _make_selector([])
    ws, env = sel.split_workspace_patterns([])
    assert ws == []
    assert env == []


def test_split_workspace_patterns_all_env() -> None:
    sel = _make_selector([])
    ws, env = sel.split_workspace_patterns(["alpha/backend", "beta/worker", "*/backend"])
    assert ws == []
    assert env == ["alpha/backend", "beta/worker", "*/backend"]


def test_split_workspace_patterns_all_workspace() -> None:
    sel = _make_selector([])
    ws, env = sel.split_workspace_patterns(["workspace", "workspace/ws-backend"])
    assert ws == ["workspace", "workspace/ws-backend"]
    assert env == []


def test_split_workspace_patterns_mixed() -> None:
    sel = _make_selector([])
    ws, env = sel.split_workspace_patterns(["alpha/backend", "workspace/ws-backend", "beta/*"])
    assert ws == ["workspace/ws-backend"]
    assert env == ["alpha/backend", "beta/*"]


def test_split_workspace_patterns_glob_env_seg_is_not_workspace() -> None:
    """work* glob env-segment does NOT count as a workspace pattern."""
    sel = _make_selector([])
    ws, env = sel.split_workspace_patterns(["work*/backend"])
    assert ws == []
    assert env == ["work*/backend"]


# ---------------------------------------------------------------------------
# read_manifest_context
# ---------------------------------------------------------------------------


def test_read_manifest_context_concrete_env_from_pattern() -> None:
    """Concrete env segment in pattern seeds the manifest read."""
    sel = _make_selector(["mp-alpha"])
    result = sel.read_manifest_context(["alpha/backend"], _WORKSPACE)
    assert result is not None
    prefix, names = result
    assert prefix == "mp"
    assert "backend" in names
    assert "worker" in names


def test_read_manifest_context_glob_pattern_falls_back_to_running_env() -> None:
    """When all patterns are globs, fall back to running sessions."""
    sel = _make_selector(["mp-alpha"])
    result = sel.read_manifest_context(["*/backend"], _WORKSPACE)
    assert result is not None
    prefix, names = result
    assert prefix == "mp"
    assert "backend" in names


def test_read_manifest_context_no_sessions_returns_none() -> None:
    """No sessions and glob-only patterns → None."""
    sel = _make_selector([])
    result = sel.read_manifest_context(["*/backend"], _WORKSPACE)
    assert result is None


def test_read_manifest_context_build_error_returns_none() -> None:
    """If builder raises, return None."""
    from service_manifest.modules.manifest.errors import ManifestError

    sel = _make_selector(["mp-alpha"], build_raises=ManifestError("boom"))
    result = sel.read_manifest_context(["alpha/backend"], _WORKSPACE)
    assert result is None


def test_read_manifest_context_workspace_first_seeds_from_env() -> None:
    """REGRESSION: workspace session first in list must NOT be used as seed.

    When list_sessions() returns workspace before alpha, the seed must be
    taken from the env session (alpha) so the env service catalog is used,
    not the workspace service catalog.
    """
    # sessions: workspace first, then alpha
    tmux = _make_tmux(["mp-workspace", "mp-alpha"])
    builder = _FakeBuilder()
    sel = SelectorService(tmux, builder)  # type: ignore[arg-type]

    result = sel.read_manifest_context(["*/backend"], _WORKSPACE)
    assert result is not None
    _prefix, names = result
    # Must have seeded from alpha (env catalog: backend, worker)
    assert "backend" in names
    # Must NOT have seeded from workspace catalog (ws-backend, ws-worker)
    assert "ws-backend" not in names
    # build should have been called with "alpha", not "workspace"
    assert "workspace" not in builder.build_calls


def test_read_manifest_context_workspace_only_session_uses_workspace_as_seed() -> None:
    """When workspace is the ONLY session, it is used as the seed (last resort)."""
    tmux = _make_tmux(["mp-workspace"])
    builder = _FakeBuilder()
    sel = SelectorService(tmux, builder)  # type: ignore[arg-type]

    result = sel.read_manifest_context(["*/ws-backend"], _WORKSPACE)
    assert result is not None
    # The seed was workspace; workspace ctx has ws-backend, ws-worker
    _prefix, names = result
    assert "ws-backend" in names
    assert len(builder.build_workspace_calls) == 1


# ---------------------------------------------------------------------------
# expand_env_patterns
# ---------------------------------------------------------------------------


def test_expand_env_patterns_literal_match() -> None:
    sel = _make_selector(["mp-alpha"])
    env_svcs, dead = sel.expand_env_patterns(["alpha/backend"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert env_svcs == {"alpha": ["backend"]}
    assert dead == []


def test_expand_env_patterns_glob_svc_segment() -> None:
    """alpha/back* matches backend but not worker."""
    sel = _make_selector(["mp-alpha"])
    env_svcs, dead = sel.expand_env_patterns(["alpha/back*"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert "alpha" in env_svcs
    assert "backend" in env_svcs["alpha"]
    assert "worker" not in env_svcs["alpha"]
    assert dead == []


def test_expand_env_patterns_bare_env_all_svcs() -> None:
    """Bare env token (no /) matches all services in that env."""
    sel = _make_selector(["mp-alpha"])
    env_svcs, dead = sel.expand_env_patterns(["alpha"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert "alpha" in env_svcs
    assert set(env_svcs["alpha"]) == {"backend", "worker"}
    assert dead == []


def test_expand_env_patterns_cross_env_glob() -> None:
    """*/backend with two running envs → both envs, each with backend only."""
    sel = _make_selector(["mp-alpha", "mp-beta"])
    env_svcs, dead = sel.expand_env_patterns(["*/backend"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert "alpha" in env_svcs
    assert "beta" in env_svcs
    assert env_svcs["alpha"] == ["backend"]
    assert env_svcs["beta"] == ["backend"]
    assert dead == []


def test_expand_env_patterns_multi_pattern() -> None:
    """Two concrete patterns for different envs → separate entries."""
    sel = _make_selector(["mp-alpha", "mp-beta"])
    env_svcs, dead = sel.expand_env_patterns(["alpha/backend", "beta/worker"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert env_svcs.get("alpha") == ["backend"]
    assert env_svcs.get("beta") == ["worker"]
    assert dead == []


def test_expand_env_patterns_dead_pattern() -> None:
    """Pattern matching no services → appears in dead_patterns."""
    sel = _make_selector(["mp-alpha"])
    env_svcs, dead = sel.expand_env_patterns(["alpha/nonexistent"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert env_svcs == {}
    assert dead == ["alpha/nonexistent"]


def test_expand_env_patterns_mixed_dead_and_live() -> None:
    """One matching and one dead pattern."""
    sel = _make_selector(["mp-alpha"])
    env_svcs, dead = sel.expand_env_patterns(["alpha/backend", "alpha/ghost"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert env_svcs == {"alpha": ["backend"]}
    assert dead == ["alpha/ghost"]


def test_expand_env_patterns_dedup_service_across_patterns() -> None:
    """Multiple patterns matching the same (env, svc) pair dedup the service."""
    sel = _make_selector(["mp-alpha"])
    env_svcs, dead = sel.expand_env_patterns(["alpha/backend", "alpha/back*"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert env_svcs["alpha"].count("backend") == 1
    assert dead == []


def test_expand_env_patterns_all_svcs_glob() -> None:
    """alpha/* matches every service."""
    sel = _make_selector(["mp-alpha"])
    env_svcs, dead = sel.expand_env_patterns(["alpha/*"], _SERVICE_NAMES, _PREFIX, _WORKSPACE)
    assert set(env_svcs["alpha"]) == {"backend", "worker"}
    assert dead == []


# ---------------------------------------------------------------------------
# expand_workspace_patterns
# ---------------------------------------------------------------------------


def test_expand_workspace_patterns_bare_token_all_svcs() -> None:
    """Bare 'workspace' token (no /) → all workspace services."""
    sel = _make_selector([])
    matched, dead = sel.expand_workspace_patterns(["workspace"], _WS_SERVICE_NAMES)
    assert set(matched) == {"ws-backend", "ws-worker"}
    assert dead == []


def test_expand_workspace_patterns_specific_svc() -> None:
    """workspace/ws-backend → only ws-backend."""
    sel = _make_selector([])
    matched, dead = sel.expand_workspace_patterns(["workspace/ws-backend"], _WS_SERVICE_NAMES)
    assert matched == ["ws-backend"]
    assert dead == []


def test_expand_workspace_patterns_glob_svc() -> None:
    """workspace/ws-* matches both ws-backend and ws-worker."""
    sel = _make_selector([])
    matched, dead = sel.expand_workspace_patterns(["workspace/ws-*"], _WS_SERVICE_NAMES)
    assert set(matched) == {"ws-backend", "ws-worker"}
    assert dead == []


def test_expand_workspace_patterns_dead_pattern() -> None:
    """workspace/nonexistent → dead pattern."""
    sel = _make_selector([])
    matched, dead = sel.expand_workspace_patterns(["workspace/nonexistent"], _WS_SERVICE_NAMES)
    assert matched == []
    assert dead == ["workspace/nonexistent"]


def test_expand_workspace_patterns_dedup() -> None:
    """Multiple patterns matching the same service dedup the result."""
    sel = _make_selector([])
    matched, dead = sel.expand_workspace_patterns(["workspace/ws-backend", "workspace/ws-*"], _WS_SERVICE_NAMES)
    assert matched.count("ws-backend") == 1
    assert dead == []
