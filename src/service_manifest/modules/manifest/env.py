"""Env-file parsing and ``${VAR}`` interpolation — pure stdlib, no subprocess.

The env-file format mirrors what the bash ``up`` script sources:

* Blank lines and lines whose first non-whitespace character is ``#`` are
  ignored.
* Each remaining line has the form ``[export] KEY=value``.  The optional
  leading ``export `` is stripped before splitting.
* Values may be surrounded by single or double quotes; those are stripped.
* Only the *first* ``=`` on a line is the delimiter — values that contain
  ``=`` are preserved intact.

The parser accepts the *text content* as a string so it is trivially
unit-testable without touching the filesystem.
"""

from __future__ import annotations

import re
from collections.abc import Mapping


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` text into a ``{key: value}`` mapping.

    Blank lines and ``#``-comment lines are silently skipped.  A leading
    ``export `` token is stripped.  Single and double quotes wrapping the full
    value are removed.

    No variable expansion is performed here — values are returned verbatim.
    Lifecycle code that must match a pane's POSIX shell uses the runtime
    environment-source adapter instead of treating this static parser as an
    evaluation engine.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip optional leading "export "
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        # Strip matching outer quotes (single or double)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


# Match ${NAME} — the brace form only.  Bare $VAR is not interpolated.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate(template: str, env: Mapping[str, str]) -> tuple[str, list[str]]:
    """Substitute ``${VAR}`` placeholders in *template* using *env*.

    Only the brace form ``${NAME}`` is interpolated; bare ``$NAME`` is left
    untouched.

    Resolution:
    * A name present in *env* is replaced with its value.
    * A name **not** present in *env* is left as the literal ``${NAME}`` in the
      output (it is not replaced with an empty string).

    Returns a ``(rendered, unresolved)`` pair:
    * ``rendered`` — the template with all resolvable placeholders substituted.
    * ``unresolved`` — a list of variable names (in order of first appearance)
      whose placeholders were left in place because they were absent from *env*.
      May be empty.  The validator uses this to flag vars that can never
      be resolved.
    """
    unresolved: list[str] = []
    seen_unresolved: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in env:
            return env[name]
        if name not in seen_unresolved:
            seen_unresolved.add(name)
            unresolved.append(name)
        return match.group(0)  # leave literal ${NAME} in place

    rendered = _VAR_RE.sub(_replace, template)
    return rendered, unresolved


def referenced_vars(template: str) -> list[str]:
    """Return the ordered, deduplicated list of ``${VAR}`` names in *template*.

    Useful for the validator to enumerate which vars a URL template
    requires, so it can check them against the parsed env before running
    ``interpolate``.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _VAR_RE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_valid_env_name(name: str) -> bool:
    """Return whether *name* is a valid POSIX-style environment name."""
    return _ENV_NAME_RE.fullmatch(name) is not None


def malformed_references(template: str) -> list[str]:
    """Return malformed ``${...}`` references in *template* in source order."""
    malformed: list[str] = []
    for marker in re.finditer(r"\$\{", template):
        start = marker.start()
        if _VAR_RE.match(template, start) is not None:
            continue
        end = template.find("}", start + 2)
        if end < 0:
            end = len(template)
        malformed.append(template[start : end + 1])
    return malformed


def resolve_service_env(
    mapping: Mapping[str, str],
    env: Mapping[str, str],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Resolve a service mapping against *env*, in declaration order.

    Resolved entries are added to the environment before the next mapping
    entry, so a later value can reference an earlier one. Entries with missing
    references are not added to the resolution base and are reported as
    ``(mapping_key, variable_name)`` pairs.
    """
    effective = dict(env)
    resolved: dict[str, str] = {}
    unresolved: list[tuple[str, str]] = []
    for key, value in mapping.items():
        rendered, missing = interpolate(value, effective)
        if missing:
            unresolved.extend((key, name) for name in missing)
            continue
        resolved[key] = rendered
        effective[key] = rendered
    return resolved, unresolved
