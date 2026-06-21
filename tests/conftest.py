"""Shared test fakes for the orchestrate module seams.

Each fake satisfies its Protocol structurally.  A ``_conforms_*`` module-
level sentinel function acts as a typecheck-time assertion that the fake
fully implements the Protocol seam it replaces.

Fakes carry public lists / dicts so tests assert against captured call state
directly, without ``mock.call(...)`` chains.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from service_orchestrator.core.workspace_locator import IWorkspaceLocator
from service_orchestrator.modules.orchestrate.errors import OrchestratorError
from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner
from service_orchestrator.modules.orchestrate.log_repository import ILogRepository
from service_orchestrator.modules.orchestrate.reaper import IProcessReaper
from service_orchestrator.modules.orchestrate.tmux_repository import ITmuxRepository, PaneInfo

# ---------------------------------------------------------------------------
# FakeTmuxRepository
# ---------------------------------------------------------------------------


class FakeTmuxRepository:
    """In-memory ITmuxRepository.

    Internal model: ``{session: {target: pid}}`` mapping.  Exposes public
    attrs so tests can inspect state and seed initial conditions.

    ``sent`` records every ``send_keys`` call as ``(session, target, line)``.
    ``capture_text`` maps ``target`` to a canned string returned by
    ``capture_pane``.
    """

    def __init__(
        self,
        sessions: dict[str, dict[str, int]] | None = None,
        capture_text: dict[str, str] | None = None,
    ) -> None:
        # {session: {target: pid}}
        self._sessions: dict[str, dict[str, int]] = dict(sessions or {})
        self.capture_text: dict[str, str] = dict(capture_text or {})
        self.sent: list[tuple[str, str, str]] = []
        self.created_sessions: list[str] = []
        self.killed_sessions: list[str] = []

    # -- seeding helpers --

    def seed_session(self, session: str, panes: dict[str, int]) -> None:
        """Add *panes* (``{target: pid}``) to *session* (creates if absent)."""
        self._sessions[session] = dict(panes)

    def add_pane(self, session: str, target: str, pid: int) -> None:
        """Add a single pane to an existing session (used by hook runners)."""
        self._sessions.setdefault(session, {})[target] = pid

    def add_window(self, session: str) -> None:
        """Record that the hook added a window (used only for window-count checks)."""
        # Panes are added via add_pane; this is a no-op counter for window count.
        # The list_windows implementation counts unique window indices from pane targets.

    # -- Protocol methods --

    def has_session(self, session: str) -> bool:
        return session in self._sessions

    def list_sessions(self) -> list[str]:
        return list(self._sessions)

    def new_session(self, session: str, cwd: Path, width: int, height: int) -> None:
        self._sessions[session] = {}
        self.created_sessions.append(session)

    def kill_session(self, session: str) -> None:
        self._sessions.pop(session, None)
        self.killed_sessions.append(session)

    def list_windows(self, session: str) -> list[str]:
        """Return unique window indices present in *session*'s panes."""
        panes = self._sessions.get(session, {})
        windows: list[str] = []
        seen: set[str] = set()
        for target in panes:
            window_idx = target.split(".")[0]
            if window_idx not in seen:
                seen.add(window_idx)
                windows.append(window_idx)
        return windows

    def list_panes(self, session: str) -> list[PaneInfo]:
        panes = self._sessions.get(session, {})
        return [PaneInfo(target=t, pid=p) for t, p in panes.items()]

    def send_keys(self, session: str, target: str, line: str) -> None:
        self.sent.append((session, target, line))

    def capture_pane(self, session: str, target: str) -> str:
        return self.capture_text.get(target, "")


def _conforms_fake_tmux_repository(x: FakeTmuxRepository) -> ITmuxRepository:
    """Typecheck-time sentinel: FakeTmuxRepository satisfies ITmuxRepository."""
    return x


# ---------------------------------------------------------------------------
# FakeProcessReaper
# ---------------------------------------------------------------------------


class FakeProcessReaper:
    """In-memory IProcessReaper.

    ``descendant_map`` seeds ``descendants(pid)`` → ``[child_pids]``.
    ``children_set`` seeds ``has_children(pid)`` → ``True/False``.
    ``killed`` records every ``reap_descendants`` call's collected pid list.
    """

    def __init__(
        self,
        descendant_map: dict[int, list[int]] | None = None,
        children_set: set[int] | None = None,
    ) -> None:
        self._descendants: dict[int, list[int]] = dict(descendant_map or {})
        self._children: set[int] = set(children_set or set())
        self.killed: list[list[int]] = []

    def descendants(self, pid: int) -> list[int]:
        return list(self._descendants.get(pid, []))

    def has_children(self, pid: int) -> bool:
        return pid in self._children

    def reap_descendants(self, root_pids: list[int]) -> None:
        pids: list[int] = []
        for root in root_pids:
            pids.extend(self._descendants.get(root, []))
        if pids:
            self.killed.append(pids)


def _conforms_fake_process_reaper(x: FakeProcessReaper) -> IProcessReaper:
    """Typecheck-time sentinel: FakeProcessReaper satisfies IProcessReaper."""
    return x


# ---------------------------------------------------------------------------
# FakeLayoutHookRunner
# ---------------------------------------------------------------------------


class FakeLayoutHookRunner:
    """In-memory ILayoutHookRunner.

    ``run`` calls are recorded in ``self.calls`` as ``(hook_path, env, cwd)``.
    Set ``raise_on_run`` to an ``OrchestratorError`` instance to make ``run``
    raise it.  Optionally supply a ``side_effect`` callable that is called on
    each successful ``run`` invocation so tests can mutate the fake session
    (adding panes the hook would create).
    """

    def __init__(
        self,
        raise_on_run: OrchestratorError | None = None,
        side_effect: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[tuple[Path, dict[str, str], Path]] = []
        self._raise: OrchestratorError | None = raise_on_run
        self._side_effect: Callable[[], None] | None = side_effect

    def run(self, hook_path: Path, env: dict[str, str], cwd: Path) -> None:
        if self._raise is not None:
            raise self._raise
        self.calls.append((hook_path, env, cwd))
        if self._side_effect is not None:
            self._side_effect()


def _conforms_fake_layout_hook_runner(x: FakeLayoutHookRunner) -> ILayoutHookRunner:
    """Typecheck-time sentinel: FakeLayoutHookRunner satisfies ILayoutHookRunner."""
    return x


# ---------------------------------------------------------------------------
# FakeWorkspaceLocator
# ---------------------------------------------------------------------------


class FakeWorkspaceLocator:
    """In-memory IWorkspaceLocator.

    Returns the supplied *root* from ``workspace_root()``; ``worktree_dir``
    joins the env name under *root*.  ``config_dir`` returns *config_dir* when
    supplied, otherwise ``<root>/.winter/config/winter-service-tmux``.
    """

    def __init__(self, root: Path, config_dir: Path | None = None) -> None:
        self._root = root
        self._config_dir = config_dir if config_dir is not None else root / ".winter" / "config" / "winter-service-tmux"

    def workspace_root(self) -> Path:
        return self._root

    def worktree_dir(self, env: str) -> Path:
        return self._root / env

    def config_dir(self) -> Path:
        return self._config_dir


def _conforms_fake_workspace_locator(x: FakeWorkspaceLocator) -> IWorkspaceLocator:
    """Typecheck-time sentinel: FakeWorkspaceLocator satisfies IWorkspaceLocator."""
    return x


# ---------------------------------------------------------------------------
# FakeLogRepository
# ---------------------------------------------------------------------------


class FakeLogRepository:
    """In-memory ILogRepository.

    ``ensure_log_dir_calls`` records every ``ensure_log_dir`` call as the
    *worktree_dir* argument.  ``log_dir`` and ``log_path`` return computed
    paths without touching the real filesystem.

    For read methods:
    - ``segments`` maps ``service_name`` to an ordered list of segment
      content strings (oldest first), one entry per segment file.
    - ``segment_files`` returns fake Paths whose ``name`` identifies the
      service and segment index; ``read_lines`` resolves the canned content
      by looking up the path in ``_path_to_lines``, which is populated when
      ``segments`` is set.
    """

    def __init__(
        self,
        segments: dict[str, list[str]] | None = None,
    ) -> None:
        self.ensure_log_dir_calls: list[Path] = []
        # Map of service name -> list of segment content strings (oldest first).
        self._segments: dict[str, list[str]] = segments or {}
        # Internal: path -> lines (built lazily in segment_files).
        self._path_to_lines: dict[Path, list[str]] = {}
        # Follow-mode state: path -> live content string and size.
        self._live_content: dict[Path, str] = {}
        self._file_sizes: dict[Path, int] = {}
        # Prune support: service -> list of rotated segment Paths; path -> mtime; deleted paths.
        self._rotated_segments: dict[str, list[Path]] = {}
        self._mtimes: dict[Path, float] = {}
        self.deleted: list[Path] = []

    def log_dir(self, worktree_dir: Path) -> Path:
        return worktree_dir / ".winter" / "logs"

    def ensure_log_dir(self, worktree_dir: Path) -> Path:
        self.ensure_log_dir_calls.append(worktree_dir)
        return self.log_dir(worktree_dir)

    def log_path(self, worktree_dir: Path, service: str) -> Path:
        return self.log_dir(worktree_dir) / f"{service}.log"

    def segment_files(self, worktree_dir: Path, service: str) -> list[Path]:
        """Return fake segment paths for *service*, oldest first.

        Paths are synthetic (non-existent on disk); content is registered in
        ``_path_to_lines`` so ``read_lines`` can look them up.
        """
        contents = self._segments.get(service, [])
        base = self.log_path(worktree_dir, service)
        paths: list[Path] = []
        for i, content in enumerate(contents):
            if i == len(contents) - 1:
                # Newest segment — the active file (no number suffix).
                path = base
            else:
                # Older segments: oldest = highest number.
                # contents[0] is oldest → suffix = len(contents) - 1 - i
                suffix = len(contents) - 1 - i
                path = Path(f"{base}.{suffix}")
            self._path_to_lines[path] = [line.rstrip("\n") for line in content.splitlines() if line.rstrip("\n")]
            if path == base:
                # Production reads the active file's backlog via
                # ``read_new_lines(base, 0)`` (not ``read_lines``), so the
                # follow loop can seed from the exact byte boundary consumed.
                # Mirror reality: serve the active segment through the live
                # store too, so backlog + follow share one source. A pre-seed
                # from ``seed_live_content`` is overwritten here, matching the
                # real file state at the moment ``segment_files`` is consulted.
                self._live_content[base] = content
                self._file_sizes[base] = len(content)
            paths.append(path)
        return paths

    def read_lines(self, path: Path) -> list[str]:
        """Return canned lines for *path* registered by ``segment_files``."""
        return list(self._path_to_lines.get(path, []))

    def file_size(self, path: Path) -> int:
        """Return canned size for *path*, or 0 if not registered."""
        return self._file_sizes.get(path, 0)

    def read_new_lines(self, path: Path, offset: int) -> tuple[list[str], int]:
        """Return lines from the live-content queue for *path* since *offset*.

        ``set_live_content(path, content_string)`` seeds the content.
        Each call returns lines starting at *offset* (byte-counted by ASCII
        for simplicity), advancing the offset past the last complete line.

        For fake purposes we treat each character as one byte.
        """
        content = self._live_content.get(path, "")
        if offset >= len(content):
            return [], offset
        chunk = content[offset:]
        last_nl = chunk.rfind("\n")
        if last_nl == -1:
            return [], offset
        complete = chunk[: last_nl + 1]
        lines = [line for line in complete.splitlines() if line]
        new_offset = offset + len(complete)
        return lines, new_offset

    # -- seeding helpers for follow tests --

    def seed_live_content(self, path: Path, content: str) -> None:
        """Set the canned live content for *path* (used by follow tests)."""
        self._live_content[path] = content
        self._file_sizes[path] = len(content)

    # -- prune support --

    def rotated_segments(self, worktree_dir: Path, service: str) -> list[Path]:
        """Return canned rotated segment paths registered via ``seed_rotated_segments``."""
        return list(self._rotated_segments.get(service, []))

    def mtime(self, path: Path) -> float:
        """Return canned mtime for *path* registered via ``seed_mtime``."""
        return self._mtimes.get(path, 0.0)

    def delete(self, path: Path) -> None:
        """Record *path* as deleted (does not touch disk)."""
        self.deleted.append(path)

    def seed_rotated_segments(self, service: str, paths: list[Path]) -> None:
        """Register canned rotated segment paths for *service*."""
        self._rotated_segments[service] = list(paths)

    def seed_mtime(self, path: Path, mtime: float) -> None:
        """Register a canned mtime for *path*."""
        self._mtimes[path] = mtime


def _conforms_fake_log_repository(x: FakeLogRepository) -> ILogRepository:
    """Typecheck-time sentinel: FakeLogRepository satisfies ILogRepository."""
    return x


# ---------------------------------------------------------------------------
# FakeFollowClock
# ---------------------------------------------------------------------------


class FakeFollowClock:
    """Deterministic IFollowClock for follow-loop unit tests.

    ``tick_results`` is a list of booleans: each call to ``interrupted()``
    pops from the front.  When the list is exhausted, ``interrupted()``
    returns ``True`` (safe default: exits the loop).

    ``sleep_calls`` records every ``sleep(seconds)`` call.
    ``install_called`` tracks whether ``install()`` was invoked.
    ``current_time`` is the value returned by ``now()``; set it to control
    prune cutoff calculations deterministically.
    """

    def __init__(self, tick_results: list[bool] | None = None, current_time: float = 0.0) -> None:
        # Each entry answers "am I interrupted?" for one loop iteration.
        self._ticks: list[bool] = list(tick_results or [])
        self.sleep_calls: list[float] = []
        self.install_called: bool = False
        self.current_time: float = current_time

    def install(self) -> None:
        self.install_called = True

    def interrupted(self) -> bool:
        if not self._ticks:
            return True
        return self._ticks.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)

    def now(self) -> float:
        """Return the canned current time."""
        return self.current_time


def _conforms_fake_follow_clock(x: FakeFollowClock) -> IFollowClock:
    """Typecheck-time sentinel: FakeFollowClock satisfies IFollowClock."""
    return x
