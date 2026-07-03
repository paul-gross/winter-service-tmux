"""Core orchestrator service — ``up``, ``down``, ``status``, ``restart``.

All four actions share a single ``OrchestratorService`` instance.  Both CLI
doors (``cli.py`` and ``env_cli.py``, Phase 3) build an ``SessionContext`` and
delegate here.  No subprocess calls live in this file — everything is
delegated to the injected Protocol seams.
"""

from __future__ import annotations

import os
import sys
from typing import IO

from service_manifest.modules.manifest.model import HealthType, LogMode, Service, parse_port_expression
from service_orchestrator.core.winter_cli import DependencyStatus, IWinterCli, WinterCliUnavailableError
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.health_checker import IHealthChecker
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
from service_orchestrator.modules.orchestrate.reaper import IProcessReaper
from service_orchestrator.modules.orchestrate.session_context import SessionContext
from service_orchestrator.modules.orchestrate.session_context_builder import WORKSPACE_TARGET
from service_orchestrator.modules.orchestrate.status_report import (
    build_env_status,
    build_launch_line,
    build_service_status,
    last_non_blank_line,
    truncate_status_line,
)
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository

_TMUX_WIDTH = 200
_TMUX_HEIGHT = 50

# Post-launch settle window before the first liveness check in _await_startup.
# Gives the pane shell a moment to fork the service process after send_keys.
_STARTUP_SETTLE_SECONDS = 0.5

# Bounded tail read for a 'log'-type health probe against a FILE-mode log —
# large enough to catch a ready-line without loading the whole file.
_LOG_HEALTH_TAIL_BYTES = 65536

# Per-dependency depends_on readiness-gate ceiling, mirroring winter-cli's
# `up --wait` DEFAULT_WAIT_TIMEOUT_S convention (winter's own gate is a
# separate, winter-side mechanism — this is the tmux orchestrator's own
# in-process wait before launching a dependent service's pane). Overridable
# per-invocation via the caller's `--timeout`, which winter core injects as
# `WINTER_SERVICE_TIMEOUT` on `up` — see `_depends_on_timeout_seconds`.
_DEPENDS_ON_TIMEOUT_SECONDS = 120.0

# Env var winter core injects on `up` carrying the effective `--wait` timeout
# (seconds, float) so the depends_on gate can honor a caller-supplied ceiling
# instead of always waiting out the hardcoded default.
_DEPENDS_ON_TIMEOUT_ENV_VAR = "WINTER_SERVICE_TIMEOUT"

# Delay between depends_on status polls, mirroring winter-cli's
# DEFAULT_POLL_INTERVAL_S.
_DEPENDS_ON_POLL_INTERVAL_SECONDS = 1.0


def _resolve_service_port(port: int | str | None, port_base: int | None) -> int | None:
    """Resolve a service's declared ``port`` field to an absolute port number.

    Returns ``None`` when *port* is ``None`` (undeclared) or when the expression
    requires *port_base* but it is ``None`` (no ``WINTER_PORT_BASE`` in the env).

    Accepts:
    - ``None`` → ``None`` (no port declared).
    - ``int`` → returned as-is (literal port).
    - ``str`` matching ``"WINTER_PORT_BASE + <offset>"`` → ``port_base + offset``
      when *port_base* is available, else ``None``.
    """
    if port is None:
        return None
    if isinstance(port, int):
        return port
    offset = parse_port_expression(port)
    if offset is not None and port_base is not None:
        return port_base + offset
    return None


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


def _local_dep_name(dep: str, scope: str) -> str | None:
    """Normalize a ``depends_on`` entry to a bare same-scope name, or ``None`` if not local.

    A bare pattern (no ``"/"``) is already local. A qualified pattern
    (``"<scope>/<svc>"``) is ALSO local when its scope segment equals *scope*
    (the current dispatch scope, e.g. ``ctx.env`` — a feature-env name or
    ``"workspace"``) — a same-scope dependency spelled with a redundant
    self-qualification (e.g. a service in env "alpha" declaring
    ``"alpha/builder"`` instead of bare ``"builder"``) resolves to the
    identical poll target as the bare form, so it must be sequenced
    identically too. A qualified pattern naming a DIFFERENT scope (e.g.
    ``"workspace/db"`` from a project-scope service) is not local — ``None``.
    """
    if "/" not in dep:
        return dep
    prefix = f"{scope}/"
    return dep[len(prefix) :] if dep.startswith(prefix) else None


def _topological_order(services: tuple[Service, ...], scope: str) -> list[Service]:
    """Return *services* reordered so every same-scope ``depends_on`` edge is launched-before.

    A ``depends_on`` entry participates in the ordering when it resolves to a
    LOCAL name (see ``_local_dep_name``) — either a bare pattern (no ``"/"``)
    or a ``"<scope>/<svc>"`` pattern whose scope segment equals *scope* — that
    also names another service IN ``services``. A genuinely cross-scope/
    cross-provider pattern (e.g. a project-scope service depending on
    ``"workspace/db"``) or a pattern naming no service in ``services`` has no
    effect on launch order (it is still gated at launch time via the
    readiness barrier; it just cannot be locally sequenced).

    Kahn's algorithm, processing ready nodes in declaration order for a
    deterministic, stable result. Defensive against a cycle slipping past the
    validator: any node never reached by the algorithm is appended at the end
    in declaration order rather than looping forever.
    """
    by_name = {s.name: s for s in services}
    names = set(by_name)
    local_deps: dict[str, list[str]] = {}
    for s in services:
        deps = []
        for dep in s.depends_on:
            local = _local_dep_name(dep, scope)
            if local is None or local == s.name or local not in names:
                continue
            deps.append(local)
        local_deps[s.name] = deps

    in_degree: dict[str, int] = {name: len(deps) for name, deps in local_deps.items()}
    dependents: dict[str, list[str]] = {name: [] for name in by_name}
    for name, deps in local_deps.items():
        for dep in deps:
            dependents[dep].append(name)

    ready = [s.name for s in services if in_degree[s.name] == 0]
    ordered: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for dependent in dependents[name]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(services):
        seen = set(ordered)
        ordered.extend(s.name for s in services if s.name not in seen)

    return [by_name[name] for name in ordered]


def _dependency_ready(status: DependencyStatus | None) -> bool:
    """Health-then-state readiness rule for a ``depends_on`` gate.

    Ready when ``health == "healthy"``, or when ``health == "unknown"`` (no
    declared probe) and ``state == "running"`` (liveness fallback). A ``None``
    status (subprocess failure, unparseable output, or no matching service in
    the document) and an ``"unhealthy"``/``"stopped"`` report both keep the
    dependent waiting.
    """
    if status is None:
        return False
    if status.health == "healthy":
        return True
    return status.health == "unknown" and status.state == "running"


def _depends_on_timeout_seconds() -> float:
    """Resolve the depends_on gate's per-dependency timeout ceiling.

    Reads ``WINTER_SERVICE_TIMEOUT`` (injected by winter core on ``up`` with
    the caller's effective ``--wait`` ``--timeout``) from ``os.environ`` and
    parses it as a float number of seconds. Falls back to
    ``_DEPENDS_ON_TIMEOUT_SECONDS`` when the var is unset, empty, non-numeric,
    or non-positive — keeping this provider correct when driven by an older
    core that never injects it.
    """
    raw = os.environ.get(_DEPENDS_ON_TIMEOUT_ENV_VAR)
    if not raw:
        return _DEPENDS_ON_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEPENDS_ON_TIMEOUT_SECONDS
    return value if value > 0 else _DEPENDS_ON_TIMEOUT_SECONDS


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
        health_checker: IHealthChecker | None = None,
        clock: IFollowClock | None = None,
        winter_cli: IWinterCli | None = None,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
    ) -> None:
        self._tmux = tmux
        self._reaper = reaper
        self._hook_runner = hook_runner
        self._log_repo = log_repo
        self._health_checker = health_checker
        self._clock = clock
        self._winter_cli = winter_cli
        self._stdout: IO[str] = stdout if stdout is not None else sys.stdout
        self._stderr: IO[str] = stderr if stderr is not None else sys.stderr

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    def up(self, ctx: SessionContext, *, retry: bool = False) -> int:
        """Start services for *ctx.env*.

        Idempotent: if the session already exists, prints a message and
        returns 0.  On hook failure the session is salvaged when more than
        one window exists; otherwise it is torn down and ``OrchestratorError``
        is raised.

        When *retry* is True, services that declare a ``[service.startup]``
        policy with ``retries > 0`` are monitored after launch: if a service
        exits within the settle window it is re-launched up to ``retries``
        times.  Services without a policy (or ``retries == 0``) are unaffected.
        When *retry* is False (the default, used by the env-root ``./up`` door),
        behaviour is byte-for-byte identical to before.
        """
        if self._tmux.has_session(ctx.session):
            self._stdout.write(f"Session '{ctx.session}' is already running.\n")
            self._stdout.write("Use ./status to check services, or ./down to stop.\n")
            self._stdout.flush()
            return 0

        # Prune old rotated log segments opportunistically before starting.
        # Errors are swallowed so a prune failure never blocks up().
        try:
            self._prune(ctx)
        except Exception as exc:
            self._stderr.write(f"Warning: prune failed (ignored): {exc}\n")
            self._stderr.flush()

        self._tmux.new_session(
            ctx.session,
            cwd=ctx.worktree_dir,
            width=_TMUX_WIDTH,
            height=_TMUX_HEIGHT,
        )

        hook_ok = True
        if ctx.layout_hook is not None:
            hook_path = ctx.config_dir / ctx.layout_hook
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
                    self._stdout.write(
                        f"Warning: layout hook returned an error but session "
                        f"'{ctx.session}' has {len(windows)} windows running.\n"
                    )
                    self._stdout.write("  Inspect with ./status; stop with ./down if the session is unhealthy.\n")
                    self._stdout.flush()
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

        # Build a {target: pid} map for the retry loop (populated before the
        # send loop so we capture pane PIDs as they existed after the hook).
        pane_pids: dict[str, int] = {p.target: p.pid for p in pane_infos}

        # Ensure the log directory exists before starting any captured services.
        self._log_repo.ensure_log_dir(ctx.worktree_dir)

        for svc in _topological_order(ctx.services, ctx.env):
            try:
                unmet = self._await_dependencies(ctx, svc)
            except WinterCliUnavailableError as exc:
                self._stderr.write(f"up: service '{svc.name}': {exc}\n")
                self._stderr.flush()
                return 1
            if unmet is not None:
                self._stderr.write(f"up: service '{svc.name}' timed out waiting for dependency '{unmet}'\n")
                self._stderr.flush()
                return 1
            target = f"{svc.target.window}.{svc.target.pane}"
            line = self._launch_line_for(ctx, svc)
            self._tmux.send_keys(ctx.session, target, line)

        self._stdout.write(f"Started services in tmux session '{ctx.session}'\n")
        self._stdout.flush()

        if retry:
            failed = self._await_startup(ctx, pane_pids)
            if failed:
                self._stderr.write(f"Services failed to stay up after retries: {', '.join(failed)}\n")
                self._stderr.flush()
                return 1

        return 0 if hook_ok else 1

    def _launch_line_for(self, ctx: SessionContext, svc: Service) -> str:
        """Return the captured-vs-bare launch line for *svc*.

        Captured: non-empty command AND log=FILE → writer-wrapped.
        Bare: empty command OR log!=FILE → plain launch line.

        Each pane self-sources its scope environment via
        ``eval "$(winter env <scope>)"`` when *ctx.inject_scope* is not ``None``.
        When *ctx.env_file_path* is not ``None``, the manifest machine-creds
        file is also dot-sourced after the scope eval.
        Local/env-less mode (``inject_scope=None``, ``env_file_path=None``)
        omits both prefixes.
        """
        captured = bool(svc.cmd) and svc.log == LogMode.FILE
        if captured:
            logfile = self._log_repo.log_path(ctx.worktree_dir, svc.name)
            return build_launch_line(
                ctx.worktree_dir,
                ctx.inject_scope,
                svc.name,
                svc.cmd,
                env_file_path=ctx.env_file_path,
                logfile=logfile,
                rotate_size_bytes=ctx.logs.rotate_size_bytes,
                max_rotations=ctx.logs.max_rotations,
                cwd=svc.cwd,
            )
        return build_launch_line(
            ctx.worktree_dir,
            ctx.inject_scope,
            svc.name,
            svc.cmd,
            env_file_path=ctx.env_file_path,
            cwd=svc.cwd,
        )

    def _await_dependencies(self, ctx: SessionContext, svc: Service) -> str | None:
        """Block until every ``depends_on`` pattern declared by *svc* is ready.

        A bare pattern (no ``"/"``) is qualified with the current scope
        (``ctx.env`` — a feature-env name or the ``"workspace"`` token) before
        being polled; a pattern already containing ``"/"`` (e.g.
        ``"workspace/db"``) is forwarded as-is, letting a dependency resolve to
        another provider entirely. Every dependency — same-scope tmux service
        or cross-provider — is resolved through the identical
        ``IWinterCli.service_status`` seam.

        Returns ``None`` once every dependency is ready (including the trivial
        case where *svc* declares none). Returns the first (already-qualified)
        pattern that never became ready before its per-dependency timeout.

        No-op (returns ``None`` without polling) when the ``winter_cli`` or
        ``clock`` seam is unavailable — mirrors ``_await_startup``'s
        seam-unavailable fallback.

        Propagates ``WinterCliUnavailableError`` (uncaught) when the ``winter``
        CLI itself cannot be invoked — the caller (``up``) catches it around
        this call and fails fast rather than exhausting the per-dependency
        poll timeout on a condition that can never resolve.
        """
        if self._winter_cli is None or self._clock is None:
            return None
        for dep_pattern in svc.depends_on:
            qualified = dep_pattern if "/" in dep_pattern else f"{ctx.env}/{dep_pattern}"
            if not self._wait_for_dependency(qualified):
                return qualified
        return None

    def _wait_for_dependency(self, qualified_pattern: str) -> bool:
        """Poll ``qualified_pattern`` via the winter-cli seam until ready or timed out.

        Emits a one-line "waiting for dependency" notice to stdout before the
        first poll, so a slow-to-become-ready dependency reads as an
        in-progress wait rather than a silent hang. Lets
        ``WinterCliUnavailableError`` propagate uncaught — the caller fails
        fast on that condition instead of polling out the full timeout.
        """
        assert self._winter_cli is not None
        assert self._clock is not None
        self._stdout.write(f"up: waiting for dependency '{qualified_pattern}'...\n")
        self._stdout.flush()
        deadline = self._clock.now() + _depends_on_timeout_seconds()
        while True:
            status = self._winter_cli.service_status(qualified_pattern)
            if _dependency_ready(status):
                return True
            if self._clock.now() >= deadline:
                return False
            self._clock.sleep(_DEPENDS_ON_POLL_INTERVAL_SECONDS)

    def _await_startup(self, ctx: SessionContext, pane_pids: dict[str, int]) -> list[str]:
        """Monitor startup candidates and re-launch those that die within retries.

        Returns the list of service names that never stayed up after all retries.
        Returns [] immediately when there are no candidates or when the clock
        seam is unavailable.
        """
        candidates = [
            svc
            for svc in ctx.services
            if bool(svc.cmd)
            and svc.startup is not None
            and svc.startup.retries > 0
            and f"{svc.target.window}.{svc.target.pane}" in pane_pids
        ]
        if not candidates or self._clock is None:
            return []

        self._clock.sleep(_STARTUP_SETTLE_SECONDS)

        failed: list[str] = []
        for svc in candidates:
            target = f"{svc.target.window}.{svc.target.pane}"
            pid = pane_pids[target]
            if self._reaper.has_children(pid):
                # Already alive after the settle window — no retry needed.
                continue
            # Service is dead; attempt retries.
            assert svc.startup is not None  # guaranteed by candidates filter
            retries = svc.startup.retries
            line = self._launch_line_for(ctx, svc)
            alive = False
            for attempt in range(1, retries + 1):
                # Reap any descendants the dead launch may have orphaned before
                # re-sending, mirroring restart/down: has_children sees only
                # direct children, so a lingering grandchild or defunct process
                # would otherwise let a second copy stack into the same pane.
                self._reaper.reap_descendants([pid])
                self._stdout.write(f"Retrying '{svc.name}' (attempt {attempt}/{retries}) in {ctx.session}:{target}\n")
                self._stdout.flush()
                self._tmux.send_keys(ctx.session, target, line)
                self._clock.sleep(svc.startup.retry_delay)
                if self._reaper.has_children(pid):
                    alive = True
                    break
            if not alive:
                failed.append(svc.name)
        return failed

    def down(self, ctx: SessionContext) -> int:
        """Stop all services and kill the tmux session for *ctx.env*.

        No-op (returns 0) when the session is not running.
        """
        if not self._tmux.has_session(ctx.session):
            self._stdout.write(f"No running session '{ctx.session}'\n")
            self._stdout.flush()
            return 0

        pane_infos = self._tmux.list_panes(ctx.session)
        pane_pids = [pane.pid for pane in pane_infos]
        self._reaper.reap_descendants(pane_pids)

        self._tmux.kill_session(ctx.session)
        self._stdout.write(f"Stopped services for '{ctx.env}' (session: {ctx.session})\n")
        self._stdout.flush()
        return 0

    def status_env_document(self, ctx: SessionContext, services: tuple[str, ...] = ()) -> dict:  # type: ignore[type-arg]
        """Build winter's per-env status object for *ctx.env* — no output.

        Returns the env-scoped fragment of winter's env-keyed ``status``
        document (see the ``status`` wire contract in winter's
        ``context/winter-cli/usage/service.md``).  The winter service entrypoint
        (``cli.py``) aggregates one of these per env into the top-level
        ``{"envs": [...]}`` document and serialises it once; this method writes
        nothing to stdout.

        Service ``state`` is derived from live tmux panes: ``"running"`` when
        the pane has child processes, ``"stopped"`` when it has none, and
        ``"stopped"`` when the pane is absent or the session is not running (a
        declared-but-unstarted service reads as stopped, not unknown).
        ``handle`` is the pane address (``<session>:<window>.<pane>``) when a
        live pane backs the service, else ``None``.  ``log_path`` is the
        captured-log path for file-logged services (resolvable from the
        manifest regardless of whether the session is up), else ``None``.
        ``health`` is populated from declared readiness probes and remains
        ``"unknown"`` when no probe is declared. ``ports``/``since`` are
        unpopulated per the shape-stability rule (no port tracking in tmux yet).

        Args:
            ctx: The resolved environment context.
            services: Optional tuple of service names to include.  Empty tuple
                (the default) includes every service declared in the manifest.
        """
        in_scope = ctx.services
        if services:
            requested = set(services)
            in_scope = tuple(s for s in in_scope if s.name in requested)

        session_running = self._tmux.has_session(ctx.session)
        pane_map: dict[str, int] = {}
        if session_running:
            pane_map = {p.target: p.pid for p in self._tmux.list_panes(ctx.session)}

        svc_docs: list[dict] = []  # type: ignore[type-arg]
        for svc in in_scope:
            target = f"{svc.target.window}.{svc.target.pane}"
            pane_pid = pane_map.get(target)
            if pane_pid is not None:
                running = self._reaper.has_children(pane_pid)
                state = "running" if running else "stopped"
                handle: str | None = f"{ctx.session}:{target}"
            else:
                state = "stopped"
                handle = None

            captured = bool(svc.cmd) and svc.log == LogMode.FILE
            log_path = str(self._log_repo.log_path(ctx.worktree_dir, svc.name)) if captured else None

            health = self._service_health(svc, state, ctx, pane_pid=pane_pid)
            resolved_port = _resolve_service_port(svc.port, self._port_base(ctx))
            ports = [resolved_port] if resolved_port is not None else None
            svc_docs.append(
                build_service_status(svc.name, state, health=health, handle=handle, log_path=log_path, ports=ports)
            )

        return build_env_status(ctx.env, ctx.session, self._port_base(ctx), svc_docs)

    @staticmethod
    def _port_base(ctx: SessionContext) -> int | None:
        """Resolve the scope's port base from the injected env.

        Per-env scopes read ``WINTER_PORT_BASE`` (the env's own band); the
        workspace scope reads ``WINTER_WORKSPACE_PORT_BASE`` (the index-0 band)
        — the workspace scope has no ``WINTER_PORT_BASE``, so reading it there
        would always yield ``None``.  Returns ``None`` when the relevant variable
        is unset or non-integer.
        """
        env_vars = ctx.env_vars or {}
        port_base_var = "WINTER_WORKSPACE_PORT_BASE" if ctx.env == WORKSPACE_TARGET else "WINTER_PORT_BASE"
        raw = env_vars.get(port_base_var)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def status(self, ctx: SessionContext, services: tuple[str, ...] = ()) -> int:
        """Print human-readable service status for *ctx.env*.

        This is the direct-human entrypoint used by the env-root ``./status``
        door (``env_cli.py``).  The winter service entrypoint (``cli.py``) does
        NOT render here — it calls ``status_env_document`` and lets winter own
        the table/JSON rendering.

        Renders the ``=== {env} ===`` header followed by per-service
        running/stopped/missing lines with the latest captured log line.

        Args:
            ctx: The resolved environment context.
            services: Optional tuple of service names to show.  Empty tuple
                (the default) shows all services declared in the manifest.
        """
        if not self._tmux.has_session(ctx.session):
            self._stdout.write(f"No {ctx.session} session running.\n")
            self._stdout.flush()
            return 0

        pane_infos = self._tmux.list_panes(ctx.session)
        pane_map: dict[str, int] = {p.target: p.pid for p in pane_infos}

        in_scope = ctx.services
        if services:
            requested = set(services)
            in_scope = tuple(s for s in in_scope if s.name in requested)

        self._stdout.write(f"=== {ctx.env} ===\n")

        show_health = any(svc.health is not None for svc in in_scope)

        for svc in in_scope:
            target = f"{svc.target.window}.{svc.target.pane}"
            if target not in pane_map:
                health_text = self._service_health(svc, "stopped", ctx) if svc.health is not None else "-"
                if show_health:
                    self._stdout.write(f"  {svc.name + ':':<14} {'missing':<8}  {health_text:<9}\n")
                else:
                    self._stdout.write(f"  {svc.name}:".ljust(16) + "missing\n")
                continue

            pane_pid = pane_map[target]
            running = self._reaper.has_children(pane_pid)
            status_str = "running" if running else "stopped"
            health = self._service_health(svc, status_str, ctx, pane_pid=pane_pid)
            health_text = health if svc.health is not None else "-"

            captured = self._tmux.capture_pane(ctx.session, target)
            last_line = truncate_status_line(last_non_blank_line(captured))

            if show_health:
                self._stdout.write(f"  {svc.name + ':':<14} {status_str:<8}  {health_text:<9}  {last_line}\n")
            else:
                self._stdout.write(f"  {svc.name + ':':<14} {status_str:<8}  {last_line}\n")

        self._stdout.write("\n")
        self._stdout.flush()
        return 0

    def _service_health(self, svc: Service, state: str, ctx: SessionContext, pane_pid: int | None = None) -> str:
        health = svc.health
        if health is None or self._health_checker is None:
            return "unknown"
        if state != "running":
            return "unhealthy"
        log_source = self._log_source_for_health(svc, ctx) if health.type == HealthType.LOG else None
        uptime_seconds = (
            self._reaper.child_uptime_seconds(pane_pid)
            if health.type == HealthType.UPTIME and pane_pid is not None
            else None
        )
        healthy = self._health_checker.is_healthy(
            health, ctx.env_vars, ctx.worktree_dir, log_source=log_source, uptime_seconds=uptime_seconds
        )
        return "healthy" if healthy else "unhealthy"

    def _log_source_for_health(self, svc: Service, ctx: SessionContext) -> str:
        """Return the captured-output text a ``log`` health probe matches against.

        ``LogMode.PANE`` services scan the live tmux pane buffer (the same
        source ``status`` renders); ``LogMode.FILE`` (and ``MEMORY``, for
        forward-compat) services scan a bounded tail of the captured log file
        via ``self._log_repo`` — never the whole file.
        """
        if svc.log == LogMode.PANE:
            target = f"{svc.target.window}.{svc.target.pane}"
            return self._tmux.capture_pane(ctx.session, target)
        log_path = self._log_repo.log_path(ctx.worktree_dir, svc.name)
        return self._log_repo.read_tail(log_path, _LOG_HEALTH_TAIL_BYTES)

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
            ctx.inject_scope,
            service.name,
            service.cmd,
            env_file_path=ctx.env_file_path,
            cwd=service.cwd,
        )
        self._tmux.send_keys(ctx.session, target, line)
        self._stdout.write(f"Restarted '{service_name}' in {ctx.session}:{target}\n")
        self._stdout.flush()
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
        # base inherits WINTER_ENV_INDEX, WINTER_PORT_BASE, and all other
        # core-injected vars directly from os.environ (set by core before
        # invoking this subprocess on up/down/status).  No explicit pass-through
        # from ctx.env_vars is needed: in every production call path ctx.env_vars
        # is either dict(os.environ) (identical to base's source) or None.
        base: dict[str, str] = dict(os.environ)
        base["WINTER_TMUX_SESSION"] = ctx.session
        base["WINTER_TMUX_WORKTREE_DIR"] = str(ctx.worktree_dir)
        base["WINTER_ENV"] = ctx.env
        return base
