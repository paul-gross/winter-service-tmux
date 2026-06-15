# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-env tmux session via `./up`/`./down`/`./status` scripts (plus `./restart <service>` to bounce one service), so multiple envs can run their own instances of the app side-by-side without port conflicts.

## Feature environment setup steps

This extension needs a project-specific `setup-tmux.toml` manifest to know which services to run in each feature environment. After `winter ws init` clones the extension, walk the user through [ai/workflow-setup.md](./ai/workflow-setup.md) to author `workspace:/ai/project/setup-tmux.toml` and its companion `workspace:/ai/project/layout-hook.sh`. Without these, `./up` errors out in any feature environment.

`setup-tmux.toml` is the single source of truth. It declares every service by name, tmux pane target, and start command — agents read service→target mappings directly from the `[[service]]` entries. An optional gitignored `setup-tmux.local.toml` overlay can supply per-machine overrides using the same key-based merge semantics (scalars replace; `[[service]]` and `[[status.url]]` merge keyed by `name`/`label`).

See `winter-service-tmux:/workflow/setup-tmux.toml.example` and `winter-service-tmux:/workflow/setup-tmux.local.toml.example` for annotated schema references. Validate with the bundled CLI:

```bash
PYTHONPATH=src python3 -m service_manifest.cli validate <workspace-root>
```

## Registering the orchestrator

Add `service_orchestrator = "winter-service-tmux"` as a **root-level key** to the workspace `.winter/config.toml` (see `winter-service-tmux:/winter-ext.toml` for the extension name and `workspace:/ai/winter-cli/usage/service.md` for the full `winter service` contract). `logs` is unsupported until issue #3.

## Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- **To recover a single wedged or crashed service, use `./restart <service>`** — the sanctioned alternative to a manual `kill`/`pkill` or a full `./down && ./up`. It reaps just that service's pane and re-runs its declared command, leaving every other pane in the session running. The argument is a *declared service name*, not an env or worktree (it's env-scoped like `./up`/`./down`/`./status`).
- **`./status` reports the env it's run from.** `alpha/status` (or `./status` from inside `alpha/`) lists only the `<SESSION_PREFIX>-alpha` session's services; pass `--all` (`./status --all`) for the cross-env view of every running env. There is no `<worktree-name>` argument — scope comes from *which* env's `./status` you invoke, the same way `./up`/`./down` default to their own env.
- **Run the scripts from an env dir**, not the workspace root — `alpha/up`, or `cd alpha` first. The scripts are env-root symlinks; there is no `./up`/`./down`/`./status`/`./restart` at the workspace root.
- Read pane output with `tmux capture-pane`. Per-service `<window>.<pane>` targets are declared in the `target` field of each `[[service]]` entry in `workspace:/ai/project/setup-tmux.toml`.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix is declared as `session_prefix` in `workspace:/ai/project/setup-tmux.toml`.

## Testing changed orchestrator code against a worktree

The env-root `./up`/`./down`/`./status`/`./restart` symlinks resolve to the **installed** extension (`.winter/ext/service-tmux/workflow/<script>`), not your in-progress worktree — so they run committed code until you repoint them. To exercise changed orchestrator code, override the symlink at the worktree's copy, run the real entrypoint, then restore it (using `alpha` as the example env):

```bash
readlink alpha/up                                          # record original: ../.winter/ext/service-tmux/workflow/up
ln -sfn winter-service-tmux/workflow/up alpha/up           # override -> alpha/winter-service-tmux/workflow/up (sibling-relative)
cd alpha && ./up && ./status                               # exercise via the real entrypoint
ln -sfn ../.winter/ext/service-tmux/workflow/up alpha/up   # restore — always, even if the test failed
```

Repeat per script you changed. **Restore is mandatory** — a left-over override silently makes every later service call in that env run worktree code. This is the service-orchestration case of the generic "verify against the real environment" guidance in `winter-harness:/workflows/feature-delivery.md`.

The shims (`workflow/up` etc.) are thin Python launchers that call `python3 -m service_orchestrator.env_cli <action>`. To run the package's unit tests directly, see the repo `CONTRIBUTING.md`.
