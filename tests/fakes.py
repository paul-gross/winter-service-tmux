"""In-memory test doubles for service_orchestrator seams.

FakeFilesystemReader lets reader/env tests inject pre-loaded content
without touching the real filesystem.  Existence is derived from the
content dict: a path exists iff it is a key in the dict.

FakeOrchestrator and FakeLogService are promoted from test_orchestrator_cli.py
so dispatch_service tests can reuse them without duplicating definitions.
"""

from __future__ import annotations

from pathlib import Path

from service_manifest.core.filesystem import IFilesystemReader
from service_orchestrator.modules.orchestrate.log_query import LogQuery
from service_orchestrator.modules.orchestrate.session_context import SessionContext


class FakeFilesystemReader:
    """In-memory IFilesystemReader backed by a ``dict[Path, str]``.

    A path is considered to exist iff it is present as a key.
    ``read_text`` raises ``FileNotFoundError`` for absent paths (mirrors
    ``pathlib.Path.read_text`` on a missing file).
    """

    def __init__(self, files: dict[Path, str] | None = None) -> None:
        self._files: dict[Path, str] = files or {}

    def exists(self, path: Path) -> bool:
        return path in self._files

    def read_text(self, path: Path) -> str:
        if path not in self._files:
            raise FileNotFoundError(f"FakeFilesystemReader: no such file: {path}")
        return self._files[path]


def _conforms_fake_filesystem_reader(x: FakeFilesystemReader) -> IFilesystemReader:
    """Typecheck-time sentinel: FakeFilesystemReader satisfies IFilesystemReader."""
    return x


# ---------------------------------------------------------------------------
# FakeOrchestrator
# ---------------------------------------------------------------------------


class FakeOrchestrator:
    """Controllable stand-in for OrchestratorService.

    ``service_rc`` is returned by up/down/status/restart.
    All calls are recorded on public list attributes for assertion.
    """

    def __init__(self, service_rc: int = 0) -> None:
        self._service_rc = service_rc
        self.up_calls: list[SessionContext] = []
        self.last_up_retry: bool = False
        self.last_up_services: tuple[str, ...] = ()
        self.down_calls: list[SessionContext] = []
        self.last_down_services: tuple[str, ...] = ()
        self.status_calls: list[tuple[SessionContext, tuple[str, ...]]] = []
        self.restart_calls: list[tuple[SessionContext, str]] = []

    def up(self, ctx: SessionContext, *, retry: bool = False, services: tuple[str, ...] = ()) -> int:
        self.up_calls.append(ctx)
        self.last_up_retry = retry
        self.last_up_services = services
        return self._service_rc

    def down(self, ctx: SessionContext, *, services: tuple[str, ...] = ()) -> int:
        self.down_calls.append(ctx)
        self.last_down_services = services
        return self._service_rc

    def status(self, ctx: SessionContext, services: tuple[str, ...] = ()) -> int:
        self.status_calls.append((ctx, services))
        return self._service_rc

    def status_env_document(self, ctx: SessionContext, services: tuple[str, ...] = ()) -> dict:  # type: ignore[type-arg]
        self.status_calls.append((ctx, services))
        return {
            "env": ctx.env,
            "session": ctx.session,
            "port_base": None,
            "services": [],
        }

    def restart(self, ctx: SessionContext, service_name: str) -> int:
        self.restart_calls.append((ctx, service_name))
        return self._service_rc


# ---------------------------------------------------------------------------
# FakeLogService
# ---------------------------------------------------------------------------


class FakeLogService:
    """Controllable stand-in for LogService.

    ``log_rc`` is returned by logs/follow_streams.
    All calls are recorded on public list attributes for assertion.
    """

    def __init__(self, log_rc: int = 0) -> None:
        self._log_rc = log_rc
        self.logs_calls: list[tuple[SessionContext, LogQuery]] = []
        self.follow_streams_calls: list[list] = []

    def logs(self, ctx: SessionContext, query: LogQuery) -> int:
        self.logs_calls.append((ctx, query))
        return self._log_rc

    def follow_streams(self, streams: object) -> int:
        self.follow_streams_calls.append(list(streams))  # type: ignore[arg-type]
        return self._log_rc
