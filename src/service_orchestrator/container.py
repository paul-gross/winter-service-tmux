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
from service_orchestrator.core.internal.subprocess_winter_cli import SubprocessWinterCli
from service_orchestrator.core.winter_cli import IWinterCli
from service_orchestrator.core.workspace_locator import IWorkspaceLocator
from service_orchestrator.modules.orchestrate.dispatch_service import DispatchService
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.health_checker import IHealthChecker
from service_orchestrator.modules.orchestrate.internal.cli_tmux_repository import CliTmuxRepository
from service_orchestrator.modules.orchestrate.internal.local_log_repository import LocalLogRepository
from service_orchestrator.modules.orchestrate.internal.pgrep_process_reaper import PgrepProcessReaper
from service_orchestrator.modules.orchestrate.internal.real_follow_clock import RealFollowClock
from service_orchestrator.modules.orchestrate.internal.subprocess_health_checker import SubprocessHealthChecker
from service_orchestrator.modules.orchestrate.internal.subprocess_layout_hook_runner import (
    SubprocessLayoutHookRunner,
)
from service_orchestrator.modules.orchestrate.internal.tmux_error_factory import TmuxErrorFactory
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
from service_orchestrator.modules.orchestrate.log_service import LogService
from service_orchestrator.modules.orchestrate.orchestrator_service import OrchestratorService
from service_orchestrator.modules.orchestrate.reaper import IProcessReaper
from service_orchestrator.modules.orchestrate.selector_service import SelectorService
from service_orchestrator.modules.orchestrate.session_context_builder import SessionContextBuilder
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
        health_checker: IHealthChecker | None = None,
        follow_clock: IFollowClock | None = None,
        winter_cli: IWinterCli | None = None,
        log_sink: IO[str] | None = None,
        out_sink: IO[str] | None = None,
        err_sink: IO[str] | None = None,
    ) -> None:
        self.error_factory = TmuxErrorFactory()

        self.tmux: ITmuxRepository = tmux or CliTmuxRepository(self.error_factory)
        self.reaper: IProcessReaper = reaper or PgrepProcessReaper()
        self.hook_runner: ILayoutHookRunner = hook_runner or SubprocessLayoutHookRunner()
        self.locator: IWorkspaceLocator = locator or EnvWorkspaceLocator()
        self.log_repo: ILogRepository = log_repo or LocalLogRepository()
        self.health_checker: IHealthChecker = health_checker or SubprocessHealthChecker()
        self.follow_clock: IFollowClock = follow_clock or RealFollowClock()
        self.winter_cli: IWinterCli = winter_cli or SubprocessWinterCli()

        self._sm: sm_container_mod.Container = manifest or sm_container_mod.Container()
        self.manifest_reader = self._sm.manifest_reader
        self.env_reader = self._sm.env_reader

        self.orchestrator = OrchestratorService(
            tmux=self.tmux,
            reaper=self.reaper,
            hook_runner=self.hook_runner,
            log_repo=self.log_repo,
            health_checker=self.health_checker,
            clock=self.follow_clock,
            winter_cli=self.winter_cli,
            stdout=out_sink,
            stderr=err_sink,
        )

        self.log_service = LogService(
            log_repo=self.log_repo,
            follow_clock=self.follow_clock,
            tmux=self.tmux,
            sink=log_sink,
            err_sink=err_sink,
        )

        self.session_context_builder = SessionContextBuilder(
            locator=self.locator,
            manifest_reader=self.manifest_reader,
            env_reader=self.env_reader,
        )

        self.selector = SelectorService(self.tmux, self.session_context_builder)

        self.dispatch = DispatchService(
            builder=self.session_context_builder,
            orchestrator=self.orchestrator,
            log_service=self.log_service,
        )
