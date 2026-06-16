"""Follow-mode timing seam.

``IFollowClock`` abstracts the SIGINT detection and sleep used by the follow
loop in ``LogService``.  The production adapter (``RealFollowClock``) installs
a SIGINT handler and delegates to ``time.sleep``; the test fake provides
deterministic tick control.
"""

from __future__ import annotations

from typing import Protocol


class IFollowClock(Protocol):
    """Timing and interrupt detection for the follow loop.

    ``install()`` must be called once before the follow loop starts.  After
    that, ``interrupted()`` returns ``True`` once SIGINT has been received, and
    ``sleep()`` suspends the loop between poll ticks.

    ``now()`` returns the current wall-clock time in seconds since the epoch
    (equivalent to ``time.time()``).  It is the project's authoritative time
    seam and is used by prune to compare segment mtimes against the retention
    cutoff.
    """

    def install(self) -> None:
        """Install the interrupt handler (idempotent)."""
        ...

    def interrupted(self) -> bool:
        """Return ``True`` once the interrupt signal has been received."""
        ...

    def sleep(self, seconds: float) -> None:
        """Sleep for *seconds* between poll ticks."""
        ...

    def now(self) -> float:
        """Return the current time as seconds since the epoch (``time.time()``)."""
        ...
