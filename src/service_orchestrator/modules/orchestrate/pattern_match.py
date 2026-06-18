"""Segment-aware glob pattern matching for ``<env>/<service>`` identities.

Mirrors winter's ``modules/workspace/pattern_match`` semantics so the
orchestrator's expansion agrees with winter's render-time backstop.

Segment rules
-------------
- A pattern containing ``/`` is split once on the first ``/`` into an
  ``(env_seg, svc_seg)`` pair; each segment is matched independently with
  ``fnmatch.fnmatchcase``.
- A bare pattern (no ``/``) is treated as ``<pattern>/*`` — it matches every
  service in the named env.
- ``*`` does not cross ``/``, so ``*/backend`` matches every env's backend
  service but not ``alpha/backend-worker``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence


def matches_pattern(env_name: str, svc_name: str, pattern: str) -> bool:
    """Return True if ``<env_name>/<svc_name>`` matches *pattern*.

    A bare pattern (no ``/``) is expanded to ``<pattern>/*`` before matching.
    Each segment is matched with ``fnmatch.fnmatchcase`` (case-sensitive).
    """
    if "/" not in pattern:
        pattern = f"{pattern}/*"
    env_pat, svc_pat = pattern.split("/", 1)
    return fnmatch.fnmatchcase(env_name, env_pat) and fnmatch.fnmatchcase(svc_name, svc_pat)


def matches_any_pattern(env_name: str, svc_name: str, patterns: Sequence[str]) -> bool:
    """Return True if ``<env_name>/<svc_name>`` matches any pattern in *patterns*."""
    return any(matches_pattern(env_name, svc_name, p) for p in patterns)
