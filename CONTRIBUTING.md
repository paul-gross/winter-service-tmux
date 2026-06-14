# Contributing

## Commit messages

Conventional Commits with a scope:

    <type>(<scope>): <description>

    [optional body]

- Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `style`, `ai`
- Scope: repo name or subsystem (e.g. `winter-service-tmux`, `workflow`, `agents`)
- The `/wf-commit` skill from [winter-workflow](https://github.com/paul-gross/winter-workflow) generates commits in this format

## Pre-commit checks

Bash scripts: none automated. `bash -n` and `winter doctor` catch common issues manually.

Python (`src/service_manifest/`, `tests/`): run these before pushing any Python changes. Requires Python 3.11+ (`tomllib` is stdlib from 3.11; the current dev env uses 3.12).

```bash
mise run test       # pytest — must be green
mise run lint       # ruff check
mise run typecheck  # pyright
```

The `service_manifest` runtime has no third-party dependencies (stdlib-only). The dev tooling (pytest, ruff, pyright) lives in `pyproject.toml`'s `[dependency-groups] dev` and is installed via `mise run install` (or `uv sync`).

The manifest validator is a stdlib-only runtime CLI invoked with bare `python3` (e.g. `PYTHONPATH=src python3 -m service_manifest.cli validate <dir>`); use `uv run` only for dev tooling (test/lint/typecheck), not for the runtime CLI.

## Delivery

- Default branch: `master`
- **Primary contributors** push directly to `master` whenever — no PR or review required. Allowed to rewrite history.
- **Outside contributors** are welcome — open a PR against `master` and I'll review and merge.
