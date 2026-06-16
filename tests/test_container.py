"""DI smoke test — Container resolves OrchestratorService with injected fakes.

Verifies the wiring graph does not raise on construction and exposes the
expected attributes.
"""

from __future__ import annotations

from pathlib import Path

import service_manifest.container as sm_container_mod
from service_orchestrator.container import Container
from service_orchestrator.modules.orchestrate.orchestrator_service import OrchestratorService
from tests.conftest import (
    FakeLayoutHookRunner,
    FakeLogRepository,
    FakeProcessReaper,
    FakeTmuxRepository,
    FakeWorkspaceLocator,
)
from tests.fakes import FakeFilesystemReader


def test_container_resolves_orchestrator_with_fakes() -> None:
    """Container.orchestrator is an OrchestratorService when fakes are injected."""
    tmux = FakeTmuxRepository()
    reaper = FakeProcessReaper()
    hook = FakeLayoutHookRunner()
    locator = FakeWorkspaceLocator(root=Path("/fake/workspace"))

    # Build an in-memory service_manifest container so no filesystem is touched.
    toml = 'session_prefix = "mp"\n'
    fake_fs = FakeFilesystemReader({Path("/fake/workspace/ai/project/setup-tmux.toml"): toml})
    sm = sm_container_mod.Container(fs=fake_fs)

    log_repo = FakeLogRepository()

    container = Container(
        tmux=tmux,
        reaper=reaper,
        hook_runner=hook,
        locator=locator,
        manifest=sm,
        log_repo=log_repo,
    )

    assert isinstance(container.orchestrator, OrchestratorService)
    assert container.tmux is tmux
    assert container.reaper is reaper
    assert container.hook_runner is hook
    assert container.locator is locator
    assert container.log_repo is log_repo


def test_container_default_construction_does_not_raise() -> None:
    """Container() with no overrides constructs without raising (no I/O at init)."""
    container = Container()
    assert isinstance(container.orchestrator, OrchestratorService)
