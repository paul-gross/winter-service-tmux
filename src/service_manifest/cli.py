"""CLI entrypoint — ``python -m service_manifest.cli validate <workspace-dir>``.

This module is the error-handling boundary for the service_manifest package.
It catches ``ManifestError`` (structural read failures) and converts them to a
non-zero exit with a human-readable message.  All other code lets exceptions
propagate to the caller.

Usage
-----
    python3 -m service_manifest.cli validate /path/to/workspace [--json]

Exit codes
----------
    0  — manifest present and valid (no violations).
    1  — manifest has violations OR ``ManifestError`` raised (read failure).

Output modes
------------
Human (default)
    A summary line followed by one bullet per violation, or a single
    "valid" confirmation when clean.

JSON (``--json`` flag)
    A single JSON object on stdout::

        {"ok": true, "violations": []}
        {"ok": false, "violations": ["duplicate target '0.0' …", …]}

    The ``ok`` field is ``true`` iff ``violations`` is empty.  This shape
    is easy for a ``jq`` consumer or a bash ``if [[ "$(…|jq -r .ok)" == true ]]``
    guard.  The doctor probe (Probe 5) uses this flag.

Stdlib-only
-----------
    This module (and the whole ``service_manifest`` package) requires only the
    Python standard library.  Run it with bare ``python3`` and ``PYTHONPATH=src``
    — no ``uv``, no third-party packages needed at runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from service_manifest.container import Container
from service_manifest.modules.manifest.errors import ManifestError


def _validate(workspace_dir: Path, *, use_json: bool) -> int:
    """Run read + validate for *workspace_dir*.

    Returns 0 on success (no violations), 1 on any failure.  Prints output to
    stdout (human text or JSON object).  ManifestError is caught here — the
    only place in the package that catches it.
    """
    container = Container()

    try:
        manifest = container.manifest_reader.read(workspace_dir)
    except ManifestError as exc:
        if use_json:
            print(json.dumps({"ok": False, "violations": [f"read error: {exc}"]}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    # Resolve env dict — EnvFileReader returns None when the env file is absent
    # or undeclared (the validator then skips ${VAR} checks), or a (possibly
    # empty) dict when present.  The absent-vs-present distinction lives in the
    # service, so the handler never touches the filesystem seam directly.
    env_path = (workspace_dir / manifest.env_file) if manifest.env_file is not None else None
    env = container.env_reader.resolve(env_path)

    violations = container.validator.validate(manifest, env=env)

    if use_json:
        print(json.dumps({"ok": not violations, "violations": violations}))
    else:
        if violations:
            print(f"manifest invalid — {len(violations)} violation(s):")
            for v in violations:
                print(f"  • {v}")
        else:
            print("manifest valid")

    return 1 if violations else 0


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.  Parses *argv* (defaults to ``sys.argv[1:]``) and exits."""
    parser = argparse.ArgumentParser(
        prog="python -m service_manifest.cli",
        description="Validate a winter-service-tmux manifest (setup-tmux.toml).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate the manifest in a workspace/worktree directory.",
    )
    validate_parser.add_argument(
        "workspace_dir",
        metavar="WORKSPACE_DIR",
        help="Path to the workspace or worktree root containing ai/project/setup-tmux.toml.",
    )
    validate_parser.add_argument(
        "--json",
        dest="use_json",
        action="store_true",
        default=False,
        help=('Emit a single JSON object {"ok": bool, "violations": [...]} instead of human-readable output.'),
    )

    args = parser.parse_args(argv)

    workspace_dir = Path(args.workspace_dir).resolve()
    exit_code = _validate(workspace_dir, use_json=args.use_json)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
