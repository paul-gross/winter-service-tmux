# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-env tmux session via `./up`/`./down`/`./status` scripts (plus `./restart <pattern>...` to bounce one or more services), so multiple envs can run their own instances of the app side-by-side without port conflicts.

## Feature environment setup steps

This extension needs a project-specific `setup-tmux.toml` manifest to know which services to run in each feature environment. After `winter ws init` clones the extension, walk the user through [ai/workflow-setup.md](./ai/workflow-setup.md) to author `workspace:/ai/project/setup-tmux.toml` and its companion `workspace:/ai/project/layout-hook.sh`. Without these, `./up` errors out in any feature environment.

`setup-tmux.toml` is the single source of truth. It declares every service by name, tmux pane target, and start command — agents read service→target mappings directly from the `[[service]]` entries. An optional gitignored `setup-tmux.local.toml` overlay can supply per-machine overrides using the same key-based merge semantics (scalars replace; `[[service]]` and `[[status.url]]` merge keyed by `name`/`label`).

See `winter-service-tmux:/workflow/setup-tmux.toml.example` and `winter-service-tmux:/workflow/setup-tmux.local.toml.example` for annotated schema references. Validate with the bundled CLI:

```bash
PYTHONPATH=src python3 -m service_manifest.cli validate <workspace-root>
```

## Registering the orchestrator

Add `service_orchestrator = "winter-service-tmux"` as a **root-level key** to the workspace `.winter/config.toml` (see `winter-service-tmux:/winter-ext.toml` for the extension name and `workspace:/ai/winter-cli/usage/service.md` for the full `winter service` contract).

## Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- **To recover wedged or crashed services, use `./restart <pattern>...`** — the sanctioned alternative to a manual `kill`/`pkill` or a full `./down && ./up`. It reaps each matched service's pane and re-runs its declared command, leaving every other pane in the session running. One or more `<service>` glob patterns are required; they are matched against declared service names scoped to the invoking env (e.g. `./restart backend`, `./restart 'work*'`). The orchestrator expands patterns against its own service catalog — winter forwards them verbatim without expansion.
- **`./status` reports the env it's run from.** `alpha/status` (or `./status` from inside `alpha/`) lists only the `<SESSION_PREFIX>-alpha` session's services; pass `--all` (`./status --all`) for the cross-env view of every running env. Optional `<service>` glob patterns further narrow which services are shown (e.g. `./status back*`). Note: the env-root `./status` and `./restart` doors accept *service-only* glob tokens scoped to the invoking env — not the `<env>/<service>` form used by `winter service status`. Typing `./status alpha/backend` gives a no-match; use `./status backend` instead. Scope comes from *which* env's `./status` you invoke, the same way `./up`/`./down` default to their own env.
- **Run the scripts from an env dir**, not the workspace root — `alpha/up`, or `cd alpha` first. The scripts are env-root symlinks; there is no `./up`/`./down`/`./status`/`./restart` at the workspace root.
- **To read service output, use `winter service logs <env>`** (preferred) — output is captured to `<env>/.winter/logs/<service>.log` on `up` and persists across restarts, `down`, and teardown. Logs are timestamped and searchable. Examples:

  ```bash
  winter service logs alpha              # all services, full backlog
  winter service logs alpha/backend      # one service
  winter service logs alpha/backend -f   # follow (live tail, Ctrl-C to exit)
  winter service logs alpha -n 50        # last 50 events
  winter service logs alpha --since 2026-06-15T10:00:00Z   # time-bounded
  winter service logs '*/backend'        # cross-env aggregation: backend in every running env
  ```

  The wire contract and rendering (plain lines vs. NDJSON) are winter's responsibility — see `workspace:/ai/winter-cli/usage/service.md`. There is no env-root `./logs` script; the interface is `winter service logs`.
- **Not all services are captured the same way.** Each `[[service]]` entry has a `log` field (default `"file"`) that controls how its output is captured and read:
  - `"file"` (default): stdout/stderr is captured to `<env>/.winter/logs/<svc>.log` via the capture writer; `logs` reads the persisted file (timestamped, survives `down`). Note: the live pane shows plain (uncolored) output because stdout is piped.
  - `"pane"`: the service is launched bare (TTY preserved); `logs` reads the pane buffer via `tmux capture-pane` on demand (no file persistence, no timestamps, requires a running session). Natural for interactive panes (`shell`) or services where TTY fidelity matters more than persistence.
  - `"memory"`: accepted and validated; not yet implemented — `logs` emits nothing for memory-mode services (future work).
  Services with an empty `command` (interactive panes) are always launched bare regardless of `log`.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix is declared as `session_prefix` in `workspace:/ai/project/setup-tmux.toml`.

## Log capture configuration

File-mode log output lands at `<env>/.winter/logs/<service>.log`, persists across `down` and teardown, and is size-rotated. Log behavior is configured via the `[logs]` table in `workspace:/ai/project/setup-tmux.toml`; per-machine overrides go in the gitignored `setup-tmux.local.toml`. The full key/default table and overlay semantics are documented in `winter-service-tmux:/workflow/setup-tmux.toml.example` and `winter-service-tmux:/workflow/setup-tmux.local.toml.example`.

**Note on mixed-mode output:** pane-mode events carry no timestamp and sort before file-mode events in the merged stream. When `-n N` spans both file and pane services, N is an approximation across the mixed set.

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
