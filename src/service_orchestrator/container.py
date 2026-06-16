"""Composition root — wires adapters into ``OrchestratorService``.

Plain hand-written DI: no framework, no third-party library.  Optional
overrides (``tmux=``, ``reaper=``, ``hook_runner=``, ``locator=``,
``manifest_reader=``) let callers (and tests) inject fakes without touching
real subprocess adapters.

The inner ``service_manifest.Container`` is instantiated here to supply the
``ManifestReader`` and ``EnvFileReader``; an optional ``manifest=`` override
accepts a pre-built ``service_manifest.Container`` for tests.
"""

from __future__ import annotations

from typing import IO

import service_manifest.container as sm_container_mod
from service_orchestrator.core.internal.env_workspace_locator import EnvWorkspaceLocator
from service_orchestrator.core.workspace_locator import IWorkspaceLocator
from service_orchestrator.modules.orchestrate.env_context_builder import EnvContextBuilder
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.internal.cli_tmux_repository import CliTmuxRepository
from service_orchestrator.modules.orchestrate.internal.local_log_repository import LocalLogRepository
from service_orchestrator.modules.orchestrate.internal.pgrep_process_reaper import PgrepProcessReaper
from service_orchestrator.modules.orchestrate.internal.real_follow_clock import RealFollowClock
from service_orchestrator.modules.orchestrate.internal.subprocess_layout_hook_runner import (
    SubprocessLayoutHookRunner,
)
from service_orchestrator.modules.orchestrate.internal.tmux_error_factory import TmuxErrorFactory
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
from service_orchestrator.modules.orchestrate.log_service import LogService
from service_orchestrator.modules.orchestrate.orchestrator_service import OrchestratorService
from service_orchestrator.modules.orchestrate.reaper import IProcessReaper
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository


class Container:
    """Composition root: constructs adapters and injects them into services.

    Optional override kwargs allow tests to substitute fakes for any seam
    without touching the real subprocess adapters.
    """

    def __init__(
        self,
        *,
        tmux: ITmuxRepository | None = None,
        reaper: IProcessReaper | None = None,
        hook_runner: ILayoutHookRunner | None = None,
        locator: IWorkspaceLocator | None = None,
        manifest: sm_container_mod.Container | None = None,
        log_repo: ILogRepository | None = None,
        follow_clock: IFollowClock | None = None,
        log_sink: IO[str] | None = None,
    ) -> None:
        self.error_factory = TmuxErrorFactory()

        self.tmux: ITmuxRepository = tmux or CliTmuxRepository(self.error_factory)
        self.reaper: IProcessReaper = reaper or PgrepProcessReaper()
        self.hook_runner: ILayoutHookRunner = hook_runner or SubprocessLayoutHookRunner()
        self.locator: IWorkspaceLocator = locator or EnvWorkspaceLocator()
        self.log_repo: ILogRepository = log_repo or LocalLogRepository()
        self.follow_clock: IFollowClock = follow_clock or RealFollowClock()

        self._sm: sm_container_mod.Container = manifest or sm_container_mod.Container()
        self.manifest_reader = self._sm.manifest_reader
        self.env_reader = self._sm.env_reader

        self.orchestrator = OrchestratorService(
            tmux=self.tmux,
            reaper=self.reaper,
            hook_runner=self.hook_runner,
            log_repo=self.log_repo,
            clock=self.follow_clock,
        )

        self.log_service = LogService(
            log_repo=self.log_repo,
            follow_clock=self.follow_clock,
            tmux=self.tmux,
            sink=log_sink,
        )

        self.env_context_builder = EnvContextBuilder(
            locator=self.locator,
            manifest_reader=self.manifest_reader,
            env_reader=self.env_reader,
        )
