"""Core orchestrator service — ``up``, ``down``, ``status``, ``restart``.

All four actions share a single ``OrchestratorService`` instance.  Both CLI
doors (``cli.py`` and ``env_cli.py``, Phase 3) build an ``SessionContext`` and
delegate here.  No subprocess calls live in this file — everything is
delegated to the injected Protocol seams.
"""

from __future__ import annotations

import json
import os
import sys

from service_manifest.modules.manifest.env import interpolate
from service_manifest.modules.manifest.model import LogMode
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
from service_orchestrator.modules.orchestrate.reaper import IProcessReaper
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.status_report import (
    build_launch_line,
    build_status_json,
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

    A segment that vanishes between ``rotated_segments`` and the mtime read
    (race window) is silently skipped — the file is already gone so there is
    nothing to prune.
    """
    result = []
    for path in segments:
        try:
            if log_repo.mtime(path) < cutoff:
                result.append(path)
        except FileNotFoundError:
            # File removed between listing and stat — already gone, skip it.
            pass
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

    def up(self, ctx: SessionContext) -> int:
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
            self._prune(ctx)
        except Exception as exc:
            print(f"Warning: prune failed (ignored): {exc}", file=sys.stderr, flush=True)

        self._tmux.new_session(
            ctx.session,
            cwd=ctx.worktree_dir,
            width=_TMUX_WIDTH,
            height=_TMUX_HEIGHT,
        )

        hook_ok = True
        if ctx.layout_hook is not None:
            hook_path = ctx.workspace_root / ctx.layout_hook
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
            for svc in ctx.services
            if f"{svc.target.window}.{svc.target.pane}" not in existing_targets
        ]
        if missing_targets:
            self._tmux.kill_session(ctx.session)
            raise OrchestratorError(
                f"manifest targets not found in session '{ctx.session}' after hook: " + ", ".join(missing_targets)
            )

        # Ensure the log directory exists before starting any captured services.
        self._log_repo.ensure_log_dir(ctx.worktree_dir)

        for svc in ctx.services:
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
                    rotate_size_bytes=ctx.logs.rotate_size_bytes,
                    max_rotations=ctx.logs.max_rotations,
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

    def down(self, ctx: SessionContext) -> int:
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

    def status(self, ctx: SessionContext, services: tuple[str, ...] = (), *, json_output: bool = False) -> int:
        """Print service status for *ctx.env*.

        Renders the manifest's declarative status URLs as a header (with
        ``${VAR}`` placeholders interpolated against ``ctx.env_vars``), then
        per-service running/stopped/missing lines.

        Args:
            ctx: The resolved environment context.
            services: Optional tuple of service names to show.  Empty tuple
                (the default) shows all services declared in the manifest.
            json_output: Emit a JSON verdict object instead of human-readable
                text (see below).

        When *json_output* is ``True``, emits a single JSON object to stdout
        instead of human-readable text::

            {
              "env": "alpha",
              "session": "mp-alpha",
              "running": true,
              "services": [
                {"name": "backend", "verdict": "running"},
                {"name": "frontend", "verdict": "stopped"},
                {"name": "shell", "verdict": "missing"}
              ]
            }

        ``verdict`` is one of ``"running"``, ``"stopped"``, or ``"missing"``.
        ``running`` at the top level is ``true`` iff the tmux session exists.
        The *services* filter applies to both output modes.
        """
        if not self._tmux.has_session(ctx.session):
            if json_output:
                doc = build_status_json(ctx.env, ctx.session, session_running=False, services=[])
                print(json.dumps(doc, ensure_ascii=False))
            else:
                print(f"No {ctx.session} session running.")
            return 0

        pane_infos = self._tmux.list_panes(ctx.session)
        pane_map: dict[str, int] = {p.target: p.pid for p in pane_infos}

        in_scope = ctx.services
        if services:
            requested = set(services)
            in_scope = tuple(s for s in in_scope if s.name in requested)

        if json_output:
            svc_verdicts: list[dict[str, str]] = []
            for svc in in_scope:
                target = f"{svc.target.window}.{svc.target.pane}"
                if target not in pane_map:
                    verdict = "missing"
                else:
                    pane_pid = pane_map[target]
                    verdict = "running" if self._reaper.has_children(pane_pid) else "stopped"
                svc_verdicts.append({"name": svc.name, "verdict": verdict})
            doc = build_status_json(ctx.env, ctx.session, session_running=True, services=svc_verdicts)
            print(json.dumps(doc, ensure_ascii=False))
            return 0

        print(f"=== {ctx.env} ===")

        if ctx.status_urls:
            env_vars = ctx.env_vars or {}
            for status_url in ctx.status_urls:
                rendered_url, _ = interpolate(status_url.url, env_vars)
                print(f"  {status_url.label}: {rendered_url}")

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

    def restart(self, ctx: SessionContext, service_name: str) -> int:
        """Restart a single named service in the running session.

        Raises ``OrchestratorError`` when *service_name* is not declared in
        the manifest or when the pane is not found in the running session.
        """
        # Resolve the service from the manifest.
        service = None
        for svc in ctx.services:
            if svc.name == service_name:
                service = svc
                break

        if service is None:
            declared = ", ".join(s.name for s in ctx.services)
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

    def _prune(self, ctx: SessionContext) -> None:
        """Remove rotated log segments older than the retention window.

        Internal method — called opportunistically by ``up`` before starting
        services.  Not part of the public OrchestratorService interface.

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
        cutoff = self._clock.now() - ctx.logs.retention_seconds
        for svc in ctx.services:
            segments = self._log_repo.rotated_segments(ctx.worktree_dir, svc.name)
            to_delete = _segments_to_prune(segments, self._log_repo, cutoff)
            for path in to_delete:
                self._log_repo.delete(path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_hook_env(ctx: SessionContext) -> dict[str, str]:
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
