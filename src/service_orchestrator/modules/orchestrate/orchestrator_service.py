"""Core orchestrator service — ``up``, ``down``, ``status``, ``restart``.

All four actions share a single ``OrchestratorService`` instance.  Both CLI
doors (``cli.py`` and ``env_cli.py``, Phase 3) build an ``EnvContext`` and
delegate here.  No subprocess calls live in this file — everything is
delegated to the injected Protocol seams.
"""

from __future__ import annotations

import os
import sys

from service_manifest.modules.manifest.env import interpolate
from service_manifest.modules.manifest.model import LogMode
from service_orchestrator.modules.orchestrate.env_context import EnvContext
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
from service_orchestrator.modules.orchestrate.reaper import IProcessReaper
from service_orchestrator.modules.orchestrate.status_report import (
    build_launch_line,
    last_non_blank_line,
    truncate_status_line,
)
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository

_TMUX_WIDTH = 200
_TMUX_HEIGHT = 50


def _segments_to_prune(
    segments: list,
    log_repo: ILogRepository,
    cutoff: float,
) -> list:
    """Return the subset of *segments* whose mtime is older than *cutoff*.

    Pure function: takes a list of paths, the log repo (for mtime), and a
    cutoff timestamp; returns the paths to delete.  Testable without any I/O
    by injecting a fake log repo.
    """
    result = []
    for path in segments:
        if log_repo.mtime(path) < cutoff:
            result.append(path)
    return result


class OrchestratorService:
    """Orchestrates tmux sessions for one feature environment.

    All collaborators are injected; no subprocess calls live here.
    """

    def __init__(
        self,
        tmux: ITmuxRepository,
        reaper: IProcessReaper,
        hook_runner: ILayoutHookRunner,
        log_repo: ILogRepository,
        clock: IFollowClock | None = None,
    ) -> None:
        self._tmux = tmux
        self._reaper = reaper
        self._hook_runner = hook_runner
        self._log_repo = log_repo
        self._clock = clock

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    def up(self, ctx: EnvContext) -> int:
        """Start services for *ctx.env*.

        Idempotent: if the session already exists, prints a message and
        returns 0.  On hook failure the session is salvaged when more than
        one window exists; otherwise it is torn down and ``OrchestratorError``
        is raised.
        """
        if self._tmux.has_session(ctx.session):
            print(f"Session '{ctx.session}' is already running.")
            print("Use ./status to check services, or ./down to stop.")
            return 0

        # Prune old rotated log segments opportunistically before starting.
        # Errors are swallowed so a prune failure never blocks up().
        try:
            self.prune(ctx)
        except Exception as exc:
            print(f"Warning: prune failed (ignored): {exc}", file=sys.stderr, flush=True)

        self._tmux.new_session(
            ctx.session,
            cwd=ctx.worktree_dir,
            width=_TMUX_WIDTH,
            height=_TMUX_HEIGHT,
        )

        hook_ok = True
        if ctx.manifest.layout_hook is not None:
            hook_path = ctx.workspace_root / ctx.manifest.layout_hook
            hook_env = self._build_hook_env(ctx)
            try:
                self._hook_runner.run(hook_path, hook_env, ctx.worktree_dir)
            except OrchestratorError as exc:
                windows = self._tmux.list_windows(ctx.session)
                if len(windows) <= 1:
                    self._tmux.kill_session(ctx.session)
                    raise OrchestratorError(
                        f"layout hook failed and no windows were created; session '{ctx.session}' torn down: {exc}"
                    ) from exc
                else:
                    print(
                        f"Warning: layout hook returned an error but session "
                        f"'{ctx.session}' has {len(windows)} windows running.",
                        flush=True,
                    )
                    print(
                        "  Inspect with ./status; stop with ./down if the session is unhealthy.",
                        flush=True,
                    )
                    hook_ok = False
                    # Hook failed; we kept the session but cannot safely
                    # validate panes or send service commands.
                    return 1

        # Validate every manifest service target exists as a pane.
        pane_infos = self._tmux.list_panes(ctx.session)
        existing_targets = {p.target for p in pane_infos}
        missing_targets = [
            f"{svc.name}@{svc.target.window}.{svc.target.pane}"
            for svc in ctx.manifest.services
            if f"{svc.target.window}.{svc.target.pane}" not in existing_targets
        ]
        if missing_targets:
            self._tmux.kill_session(ctx.session)
            raise OrchestratorError(
                f"manifest targets not found in session '{ctx.session}' after hook: " + ", ".join(missing_targets)
            )

        # Ensure the log directory exists before starting any captured services.
        self._log_repo.ensure_log_dir(ctx.worktree_dir)

        for svc in ctx.manifest.services:
            target = f"{svc.target.window}.{svc.target.pane}"
            # A service is captured iff it has a non-empty command AND log=FILE.
            captured = bool(svc.command) and svc.log == LogMode.FILE
            if captured:
                logfile = self._log_repo.log_path(ctx.worktree_dir, svc.name)
                line = build_launch_line(
                    ctx.worktree_dir,
                    ctx.env_file_path,
                    svc.name,
                    svc.command,
                    logfile=logfile,
                    rotate_size_bytes=ctx.manifest.logs.rotate_size_bytes,
                    max_rotations=ctx.manifest.logs.max_rotations,
                )
            else:
                line = build_launch_line(
                    ctx.worktree_dir,
                    ctx.env_file_path,
                    svc.name,
                    svc.command,
                )
            self._tmux.send_keys(ctx.session, target, line)

        print(f"Started services in tmux session '{ctx.session}'")
        return 0 if hook_ok else 1

    def down(self, ctx: EnvContext) -> int:
        """Stop all services and kill the tmux session for *ctx.env*.

        No-op (returns 0) when the session is not running.
        """
        if not self._tmux.has_session(ctx.session):
            print(f"No running session '{ctx.session}'")
            return 0

        pane_infos = self._tmux.list_panes(ctx.session)
        pane_pids = [pane.pid for pane in pane_infos]
        self._reaper.reap_descendants(pane_pids)

        self._tmux.kill_session(ctx.session)
        print(f"Stopped services for '{ctx.env}' (session: {ctx.session})")
        return 0

    def status(self, ctx: EnvContext, services: tuple[str, ...] = ()) -> int:
        """Print service status for *ctx.env*.

        Renders the manifest's declarative status URLs as a header (with
        ``${VAR}`` placeholders interpolated against ``ctx.env_vars``), then
        per-service running/stopped/missing lines.

        Args:
            ctx: The resolved environment context.
            services: Optional tuple of service names to show.  Empty tuple
                (the default) shows all services declared in the manifest.
        """
        if not self._tmux.has_session(ctx.session):
            print(f"No {ctx.session} session running.")
            return 0

        print(f"=== {ctx.env} ===")

        if ctx.manifest.status_urls:
            env_vars = ctx.env_vars or {}
            for status_url in ctx.manifest.status_urls:
                rendered_url, _ = interpolate(status_url.url, env_vars)
                print(f"  {status_url.label}: {rendered_url}")

        pane_infos = self._tmux.list_panes(ctx.session)
        pane_map: dict[str, int] = {p.target: p.pid for p in pane_infos}

        in_scope = ctx.manifest.services
        if services:
            requested = set(services)
            in_scope = tuple(s for s in in_scope if s.name in requested)

        for svc in in_scope:
            target = f"{svc.target.window}.{svc.target.pane}"
            if target not in pane_map:
                print(f"  {svc.name}:".ljust(16) + "missing")
                continue

            pane_pid = pane_map[target]
            running = self._reaper.has_children(pane_pid)
            status_str = "running" if running else "stopped"

            captured = self._tmux.capture_pane(ctx.session, target)
            last_line = truncate_status_line(last_non_blank_line(captured))

            print(f"  {svc.name + ':':<14} {status_str:<8}  {last_line}")

        print()
        return 0

    def restart(self, ctx: EnvContext, service_name: str) -> int:
        """Restart a single named service in the running session.

        Raises ``OrchestratorError`` when *service_name* is not declared in
        the manifest or when the pane is not found in the running session.
        """
        # Resolve the service from the manifest.
        service = None
        for svc in ctx.manifest.services:
            if svc.name == service_name:
                service = svc
                break

        if service is None:
            declared = ", ".join(s.name for s in ctx.manifest.services)
            raise OrchestratorError(f"unknown service '{service_name}'; declared services: {declared}")

        target = f"{service.target.window}.{service.target.pane}"

        pane_infos = self._tmux.list_panes(ctx.session)
        pane_map: dict[str, int] = {p.target: p.pid for p in pane_infos}

        if target not in pane_map:
            raise OrchestratorError(
                f"pane '{target}' for service '{service_name}' not found in session '{ctx.session}'"
            )

        pane_pid = pane_map[target]
        self._reaper.reap_descendants([pane_pid])

        line = build_launch_line(
            ctx.worktree_dir,
            ctx.env_file_path,
            service.name,
            service.command,
        )
        self._tmux.send_keys(ctx.session, target, line)
        print(f"Restarted '{service_name}' in {ctx.session}:{target}")
        return 0

    def prune(self, ctx: EnvContext) -> None:
        """Remove rotated log segments older than the retention window.

        Only operates when the session is NOT running (``tmux.has_session``
        returns ``False``).  Deletes segments where
        ``mtime < clock.now() - retention_seconds``.  The active
        ``<svc>.log`` is never touched — only ``<svc>.log.N`` rotated files.

        A missing clock (``self._clock is None``) is a no-op; callers that
        need prune should inject the clock via the constructor.
        """
        if self._clock is None:
            return
        if self._tmux.has_session(ctx.session):
            return
        cutoff = self._clock.now() - ctx.manifest.logs.retention_seconds
        for svc in ctx.manifest.services:
            segments = self._log_repo.rotated_segments(ctx.worktree_dir, svc.name)
            to_delete = _segments_to_prune(segments, self._log_repo, cutoff)
            for path in to_delete:
                self._log_repo.delete(path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_hook_env(ctx: EnvContext) -> dict[str, str]:
        """Build the environment dict passed to the layout hook.

        Provides the WINTER_TMUX_* contract documented in
        ``workflow/layout-hook.sh.example``.
        """
        base: dict[str, str] = dict(os.environ)
        base["WINTER_TMUX_SESSION"] = ctx.session
        base["WINTER_TMUX_WORKTREE_DIR"] = str(ctx.worktree_dir)
        base["WINTER_ENV"] = ctx.env
        # WINTER_ENV_INDEX and WINTER_PORT_BASE are set in .winter.env when
        # present; pass them through from env_vars if available.
        env_vars = ctx.env_vars or {}
        for key in ("WINTER_ENV_INDEX", "WINTER_PORT_BASE"):
            if key in env_vars:
                base[key] = env_vars[key]
        return base
