"""Production adapter for ``IFollowClock``.

Installs a ``SIGINT`` handler that sets an internal flag so the follow loop
can exit cleanly (returning exit code 130) rather than raising
``KeyboardInterrupt``.  ``sleep`` delegates to ``time.sleep``.
"""

from __future__ import annotations

import signal
import time

from service_orchestrator.modules.orchestrate.follow_clock import IFollowClock


class RealFollowClock:
    """SIGINT-aware clock for the follow loop.

    Call ``install()`` once before starting the loop.  The handler sets
    ``_interrupted`` to ``True`` on the first SIGINT, leaving the loop to
    detect it on the next ``interrupted()`` check.

    The previous SIGINT handler is restored when the process exits normally
    (install is additive; no explicit uninstall is needed for a process that
    will exit on interrupt).
    """

    def __init__(self) -> None:
        self._interrupted: bool = False

    def install(self) -> None:
        """Register the SIGINT handler (idempotent — safe to call multiple times)."""

        def _handler(signum: int, frame: object) -> None:
            self._interrupted = True

        signal.signal(signal.SIGINT, _handler)

    def interrupted(self) -> bool:
        """Return ``True`` once SIGINT has been received."""
        return self._interrupted

    def sleep(self, seconds: float) -> None:
        """Sleep for *seconds* between poll ticks."""
        time.sleep(seconds)

    def now(self) -> float:
        """Return the current time as seconds since the epoch."""
        return time.time()


def _conforms_real_follow_clock(x: RealFollowClock) -> IFollowClock:
    return x
