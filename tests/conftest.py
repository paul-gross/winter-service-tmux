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
from service_orchestrator.modules.orchestrate.layout_hook_runner import ILayoutHookRunner
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
    ``killed`` records every ``term_then_kill`` call's pid list.
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

    def term_then_kill(self, pids: list[int]) -> None:
        self.killed.append(list(pids))


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
    joins the env name under *root*.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def workspace_root(self) -> Path:
        return self._root

    def worktree_dir(self, env: str) -> Path:
        return self._root / env


def _conforms_fake_workspace_locator(x: FakeWorkspaceLocator) -> IWorkspaceLocator:
    """Typecheck-time sentinel: FakeWorkspaceLocator satisfies IWorkspaceLocator."""
    return x
