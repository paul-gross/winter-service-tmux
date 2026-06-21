"""Dispatch service — executes orchestrator/log actions per env or workspace.

Owns the shared per-target skeleton:

    build ctx via build_for_target → call service → fold exit codes →
    print ``orchestrate: env '<env>': <exc>`` to err_sink.

``_run_for_target`` implements that skeleton once; ``status_all_envs``,
``status_env_services``, ``restart_env_services``, and ``logs_backlog`` are
routed through it.  ``logs_follow`` is genuinely different (it collects
(ctx, query) pairs then makes one ``follow_streams`` call) and is left as-is.
``cli.py`` keeps only thin glue (workspace split, dead-pattern reporting,
zero-pattern "No running sessions." guard).

Construction::

    DispatchService(builder, orchestrator, log_service, err_sink=None)

``err_sink`` defaults to ``sys.stderr``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

from service_manifest.modules.manifest.errors import ManifestError
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.log_query import LogQuery
from service_orchestrator.modules.orchestrate.log_service import LogService
from service_orchestrator.modules.orchestrate.orchestrator_service import OrchestratorService
from service_orchestrator.modules.orchestrate.selector_service import SelectorService
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import (
    WORKSPACE_TARGET,
    SessionContextBuilder,
    build_for_target,
)


class DispatchService:
    """Execute orchestrator/log actions against resolved targets.

    ``status_all_envs``, ``status_env_services``, ``restart_env_services``,
    and ``logs_backlog`` share the build-ctx / catch-errors / fold-rc pattern
    via ``_run_for_target``.  ``logs_follow`` is structurally distinct and
    is not routed through ``_run_for_target``.
    """

    def __init__(
        self,
        builder: SessionContextBuilder,
        orchestrator: OrchestratorService,
        log_service: LogService,
        err_sink: IO[str] | None = None,
    ) -> None:
        self._builder = builder
        self._orchestrator = orchestrator
        self._log_service = log_service
        self._err: IO[str] = err_sink if err_sink is not None else sys.stderr

    # ------------------------------------------------------------------
    # Private shared skeleton
    # ------------------------------------------------------------------

    def _build_ctx(
        self,
        target: str,
        workspace_root: Path | None,
        *,
        manifest_error_infix: bool = False,
    ) -> tuple[SessionContext | None, int]:
        """Build a SessionContext for *target*.

        Returns ``(ctx, 0)`` on success, ``(None, 1)`` on failure.

        When *manifest_error_infix* is True (up/down path), ManifestError
        produces ``orchestrate: env '<target>': manifest error: <exc>`` rather
        than the plain ``orchestrate: env '<target>': <exc>`` used by all other
        actions.
        """
        try:
            ctx = build_for_target(self._builder, target, workspace_root=workspace_root)
            return ctx, 0
        except ManifestError as exc:
            if manifest_error_infix:
                print(
                    f"orchestrate: env '{target}': manifest error: {exc}",
                    file=self._err,
                )
            else:
                print(f"orchestrate: env '{target}': {exc}", file=self._err)
            return None, 1
        except (OSError, OrchestratorError) as exc:
            print(f"orchestrate: env '{target}': {exc}", file=self._err)
            return None, 1

    def _build_workspace_ctx(
        self,
        workspace_root: Path | None,
        action: str,
    ) -> tuple[SessionContext | None, int]:
        """Build workspace SessionContext; print workspace-scoped error on failure."""
        try:
            ctx = build_for_target(self._builder, WORKSPACE_TARGET, workspace_root=workspace_root)
            return ctx, 0
        except (ManifestError, OSError, OrchestratorError) as exc:
            print(f"orchestrate: {action}: workspace: {exc}", file=self._err)
            return None, 1

    def _run_for_target(
        self,
        env_services: dict[str, list[str]],
        workspace_root: Path | None,
        invoke: Callable[[SessionContext, str, list[str]], int],
        *,
        current_rc: int = 0,
    ) -> int:
        """Shared per-env dispatch skeleton used by status and log-backlog methods.

        For each (env, svc_names) pair: builds ctx (folding build_rc on failure
        and continuing), then calls ``invoke(ctx, env, svc_names)`` which returns
        an int rc.  Folds last-non-zero-wins across all envs, catching
        ``OrchestratorError`` per-env and printing
        ``orchestrate: env '<env>': <exc>`` to err_sink (rc=1).
        """
        rc = current_rc
        for env, svc_names in env_services.items():
            ctx, build_rc = self._build_ctx(env, workspace_root)
            if ctx is None:
                rc = build_rc
                continue
            try:
                result = invoke(ctx, env, svc_names)
                if result != 0:
                    rc = result
            except OrchestratorError as exc:
                print(f"orchestrate: env '{env}': {exc}", file=self._err)
                rc = 1
        return rc

    # ------------------------------------------------------------------
    # up / down
    # ------------------------------------------------------------------

    def up(self, env: str, workspace_root: Path | None) -> int:
        """Build ctx for *env* and call orchestrator.up.  Returns exit code."""
        ctx, rc = self._build_ctx(env, workspace_root, manifest_error_infix=(env != WORKSPACE_TARGET))
        if ctx is None:
            return rc
        try:
            return self._orchestrator.up(ctx)
        except OrchestratorError as exc:
            print(f"orchestrate: env '{env}': {exc}", file=self._err)
            return 1

    def down(self, env: str, workspace_root: Path | None) -> int:
        """Build ctx for *env* and call orchestrator.down.  Returns exit code."""
        ctx, rc = self._build_ctx(env, workspace_root, manifest_error_infix=(env != WORKSPACE_TARGET))
        if ctx is None:
            return rc
        try:
            return self._orchestrator.down(ctx)
        except OrchestratorError as exc:
            print(f"orchestrate: env '{env}': {exc}", file=self._err)
            return 1

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status_all_envs(
        self,
        envs: list[str],
        workspace_root: Path | None,
        *,
        json_output: bool = False,
        current_rc: int = 0,
    ) -> int:
        """Call status for every env in *envs*.  Folds exit codes last-non-zero-wins."""

        def _invoke(ctx: SessionContext, env: str, svc_names: list[str]) -> int:
            return self._orchestrator.status(ctx, json_output=json_output)

        return self._run_for_target(
            {env: [] for env in envs},
            workspace_root,
            _invoke,
            current_rc=current_rc,
        )

    def status_env_services(
        self,
        env_services: dict[str, list[str]],
        workspace_root: Path | None,
        *,
        json_output: bool = False,
        current_rc: int = 0,
    ) -> int:
        """Call status for each env → [services] mapping.  Folds last-non-zero-wins."""

        def _invoke(ctx: SessionContext, env: str, svc_names: list[str]) -> int:
            return self._orchestrator.status(
                ctx,
                services=tuple(svc_names),
                json_output=json_output,
            )

        return self._run_for_target(env_services, workspace_root, _invoke, current_rc=current_rc)

    def status_workspace(
        self,
        selector: SelectorService,
        workspace_pats: list[str],
        workspace_root: Path | None,
        action: str,
        *,
        json_output: bool = False,
        current_rc: int = 0,
    ) -> int:
        """Handle status for workspace-scoped patterns.

        Builds the workspace context once, expands svc-globs against workspace
        services, and calls orchestrator.status with the matched subset.
        """
        ctx, build_rc = self._build_workspace_ctx(workspace_root, action)
        if ctx is None:
            return build_rc

        all_ws_names = [svc.name for svc in ctx.services]
        matched, dead = selector.expand_workspace_patterns(workspace_pats, all_ws_names)

        if dead:
            for pat in dead:
                print(
                    f"orchestrate: {action}: pattern '{pat}' matched no services",
                    file=self._err,
                )
            return 1

        rc = current_rc
        try:
            result = self._orchestrator.status(
                ctx,
                services=tuple(matched) if matched else (),
                json_output=json_output,
            )
            if result != 0:
                rc = result
        except OrchestratorError as exc:
            print(f"orchestrate: {action}: workspace: {exc}", file=self._err)
            rc = 1
        return rc

    # ------------------------------------------------------------------
    # restart
    # ------------------------------------------------------------------

    def restart_env_services(
        self,
        env_services: dict[str, list[str]],
        workspace_root: Path | None,
        *,
        current_rc: int = 0,
    ) -> int:
        """Call restart for each (env, svc) pair.  Folds last-non-zero-wins."""

        def _invoke(ctx: SessionContext, env: str, svc_names: list[str]) -> int:
            inner_rc = 0
            for svc_name in svc_names:
                result = self._orchestrator.restart(ctx, svc_name)
                if result != 0:
                    inner_rc = result
            return inner_rc

        return self._run_for_target(env_services, workspace_root, _invoke, current_rc=current_rc)

    def restart_workspace(
        self,
        selector: SelectorService,
        workspace_pats: list[str],
        workspace_root: Path | None,
        current_rc: int = 0,
    ) -> int:
        """Handle restart for workspace-scoped patterns."""
        ctx, build_rc = self._build_workspace_ctx(workspace_root, "restart")
        if ctx is None:
            return build_rc

        all_ws_names = [svc.name for svc in ctx.services]
        matched, dead = selector.expand_workspace_patterns(workspace_pats, all_ws_names)

        if dead:
            for pat in dead:
                print(
                    f"orchestrate: restart: pattern '{pat}' matched no services",
                    file=self._err,
                )
            return 1

        rc = current_rc
        for svc_name in matched:
            try:
                result = self._orchestrator.restart(ctx, svc_name)
                if result != 0:
                    rc = result
            except OrchestratorError as exc:
                print(f"orchestrate: restart: workspace: {exc}", file=self._err)
                rc = 1
        return rc

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    def logs_backlog(
        self,
        env_services: dict[str, list[str]],
        workspace_root: Path | None,
        log_env: Mapping[str, str],
        *,
        current_rc: int = 0,
    ) -> int:
        """Read log backlog for each (env, svc) pair.  Folds last-non-zero-wins."""

        def _invoke(ctx: SessionContext, env: str, svc_names: list[str]) -> int:
            query = LogQuery.from_env(tuple(svc_names), log_env)
            return self._log_service.logs(ctx, query)

        return self._run_for_target(env_services, workspace_root, _invoke, current_rc=current_rc)

    def logs_follow(
        self,
        env_services: dict[str, list[str]],
        workspace_root: Path | None,
        log_env: Mapping[str, str],
        current_rc: int = 0,
    ) -> int:
        """Build (ctx, query) pairs and call log_service.follow_streams.

        Pairs collection: build ctx per env; skip on error (rc=1).
        Empty pairs: return current_rc or 1.
        follow_streams result: return result if result != 0 else current_rc.
        """
        pairs: list[tuple[SessionContext, LogQuery]] = []
        rc = current_rc
        for env, svc_names in env_services.items():
            ctx, build_rc = self._build_ctx(env, workspace_root)
            if ctx is None:
                rc = build_rc
                continue
            pairs.append(
                (
                    ctx,
                    LogQuery.from_env(tuple(svc_names), log_env),
                )
            )
        if not pairs:
            return rc or 1
        try:
            result = self._log_service.follow_streams(pairs)
        except OrchestratorError as exc:
            print(f"orchestrate: follow_streams: {exc}", file=self._err)
            return 1
        return result if result != 0 else rc
