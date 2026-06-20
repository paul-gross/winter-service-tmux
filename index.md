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
- **Run the scripts from an env dir**, not the workspace root — `alpha/up`, or `cd alpha` first. The scripts are env-root symlinks; there is no `./up`/`./down`/`./status`/`./restart` at the workspace root. (The workspace scope is driven by `winter service … workspace` — see below.)
- **To read service output, use `winter service logs <env>`** (preferred) — output is captured to `<env>/.winter/logs/<service>.log` on `up` and persists across restarts, `down`, and teardown. Logs are timestamped and searchable. Examples:

  ```bash
  winter service logs alpha              # all services, full backlog
  winter service logs alpha/backend      # one service
  winter service logs alpha -n 50        # last 50 events
  winter service logs alpha --since 2026-06-15T10:00:00Z   # time-bounded
  winter service logs '*/backend'        # cross-env aggregation: backend in every running env
  winter service logs alpha -f           # follow ALL services in alpha, interleaved (Ctrl-C to stop)
  winter service logs alpha/backend -f   # follow one service
  winter service logs '*/backend' -f     # follow backend across every running env, interleaved
  ```

  `-f` follows ALL matched `(env, service)` streams concurrently until Ctrl-C, interleaving their lines into one output.

  The wire contract and rendering (plain lines vs. NDJSON) are winter's responsibility — see `workspace:/ai/winter-cli/usage/service.md`. There is no env-root `./logs` script; the interface is `winter service logs`. Workspace service logs land at `<workspace-root>/.winter/logs/<service>.log` — note that `winter service logs workspace` is not yet wired in this iteration; read the log files directly. Workspace log support is future work.
- **Not all services are captured the same way.** Each `[[service]]` entry has a `log` field (default `"file"`) that controls how its output is captured and read:
  - `"file"` (default): stdout/stderr is captured to `<env>/.winter/logs/<svc>.log` via the capture writer; `logs` reads the persisted file (timestamped, survives `down`). Note: the live pane shows plain (uncolored) output because stdout is piped.
  - `"pane"`: the service is launched bare (TTY preserved); `logs` reads the pane buffer via `tmux capture-pane` on demand (no file persistence, no timestamps, requires a running session). Natural for interactive panes (`shell`) or services where TTY fidelity matters more than persistence.
  - `"memory"`: accepted and validated; not yet implemented — `logs` emits nothing for memory-mode services (future work).
  Services with an empty `command` (interactive panes) are always launched bare regardless of `log`.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix is declared as `session_prefix` in `workspace:/ai/project/setup-tmux.toml`. A separate `<SESSION_PREFIX>-workspace` session holds workspace-scoped singleton services — see the next section.

## Workspace-scoped singleton services

Some services — a shared database, a container registry, a message broker — should run once for the whole workspace rather than once per feature env. The orchestrator supports this via a dedicated `<SESSION_PREFIX>-workspace` tmux session (e.g. `mp-workspace`) created at the **workspace root**, separate from every per-env session.

### Driving the workspace session

Use `winter service` with the reserved `workspace` target:

```bash
winter service up workspace          # create mp-workspace and launch all workspace services
winter service down workspace        # reap the workspace session and all its panes
winter service status workspace      # list workspace service states
winter service restart workspace/db  # restart a single workspace service
```

`winter service up <env>` ensures the workspace session is running before it starts the env session — so workspace singletons are guaranteed to be up when any env spins up. Note: the env-root `./up` symlink calls the orchestrator directly and does NOT auto-start the workspace session; if you use `alpha/up`, run `winter service up workspace` first, or use `winter service up alpha` instead. `down <env>` intentionally leaves the workspace session running; only `down workspace` tears it down.

`winter service status` (no patterns) and `winter service status --all` both include the workspace session alongside env sessions.

The `workspace` token is an exact reserved name — `work*` globs do NOT match it. Workspace services are scoped to `winter service` commands; there are no env-root `./up`/`./down` symlinks at the workspace root.

### Declaring workspace services

Add `scope = "workspace"` to a `[[service]]` entry in `workspace:/ai/project/setup-tmux.toml`. `scope` is the only field that distinguishes a workspace singleton from a per-env service — it defaults to `"project"` when omitted, and every other field (`name`/`target`/`command`/`log`) is identical:

```toml
workspace_layout_hook = "ai/project/workspace-layout-hook.sh"

[[service]]
name    = "db"
target  = "0.0"
command = "postgres -D /usr/local/var/postgres"
scope   = "workspace"

[[service]]
name    = "broker"
target  = "0.1"
command = "rabbitmq-server"
scope   = "workspace"
```

Key points:
- **`workspace_layout_hook`** — optional bash script invoked once after the workspace session is created, exactly like `layout_hook` for env sessions but run with `WINTER_TMUX_WORKTREE_DIR` set to the workspace root. See `winter-service-tmux:/workflow/layout-hook.sh.example` for the contract; the workspace hook follows the same constraints (layout only — no `send-keys`, no `source`, no `cd`).
- **Per-scope target namespaces** — a workspace service at `target = "0.0"` and a project service at `target = "0.0"` do NOT conflict (different tmux sessions; targets are validated per scope).
- **Global name namespace** — names are unique across both scopes; a project and a workspace service may not share a name.
- **Overlay merging** — `setup-tmux.local.toml` merges all `[[service]]` entries by `name` (any scope) using override-or-append; an override keeps/sets its own `scope`.
- **Validation** — the bundled validator checks for duplicate names (global) and duplicate targets (per scope). Run it with `PYTHONPATH=src python3 -m service_manifest.cli validate <workspace-root>`.

See `winter-service-tmux:/workflow/setup-tmux.toml.example` and `winter-service-tmux:/workflow/setup-tmux.local.toml.example` for annotated schema references.

### Blocking-inline-pane lifecycle (best-effort shutdown)

Workspace services run **inline** (blocking) in their tmux pane — a `postgres -D ...` or `docker run ...` command occupies the pane's foreground process. `down workspace` reaps the session, terminating each pane's foreground process tree (best-effort).

**This is best-effort.** A service that double-forks, daemonizes, or detaches from the pane's process group before `down` is called may survive the session reap. The orchestrator does not track child PIDs, issue container stop commands, or perform managed/graceful shutdown — that is explicitly out of scope for this iteration. For workloads where graceful teardown matters (e.g. `docker compose down` instead of `docker compose up` kill), run the teardown logic inside the pane's start command (e.g. a wrapper script that traps SIGHUP and calls `docker compose down` before exiting).

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

**Note on `-f` in verification:** `winter service logs … -f` blocks until SIGINT — it never returns on its own. An automated verifier must bound it: `timeout -s INT 10 winter service logs '*/backend' -f`. Alternatively, use the Bash tool's `run_in_background` facility and cancel when done.
