# Workspace-scoped singleton services

Some services — a shared database, a container registry, a message broker — should run once for the whole workspace rather than once per feature env. The orchestrator supports this via a dedicated `<SESSION_PREFIX>-workspace` tmux session (e.g. `mp-workspace`) created at the **workspace root**, separate from every per-env session.

## Driving the workspace session

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

## Declaring workspace services

Add `scope = "workspace"` to a `[[service]]` entry in `workspace:/.winter/config/winter-service-tmux/config.toml`. `scope` is the only field that distinguishes a workspace singleton from a per-env service — it defaults to `"project"` when omitted, and every other field (`name`/`target`/`command`/`log`) is identical:

```toml
workspace_layout_hook = "workspace-layout-hook.sh"

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
- **`workspace_layout_hook`** — optional bash script invoked once after the workspace session is created, exactly like `layout_hook` for env sessions but run with `WINTER_TMUX_WORKTREE_DIR` set to the workspace root. See `winter-service-tmux:/workflow/layout-hook.sh.example` for the contract; the workspace hook follows the same constraints (layout only — no `send-keys`, no `source`, no `cd`). The value is a bare filename resolved relative to the config dir (e.g. `workspace-layout-hook.sh`).
- **Per-scope target namespaces** — a workspace service at `target = "0.0"` and a project service at `target = "0.0"` do NOT conflict (different tmux sessions; targets are validated per scope).
- **Global name namespace** — names are unique across both scopes; a project and a workspace service may not share a name.
- **Overlay merging** — `config.local.toml` merges all `[[service]]` entries by `name` (any scope) using override-or-append; an override keeps/sets its own `scope`.
- **Validation** — the bundled validator checks for duplicate names (global) and duplicate targets (per scope). Run it with `PYTHONPATH=src python3 -m service_manifest.cli validate <workspace-root>`.

See `winter-service-tmux:/workflow/config.toml.example` and `winter-service-tmux:/workflow/config.local.toml.example` for annotated schema references.

## Blocking-inline-pane lifecycle (best-effort shutdown)

Workspace services run **inline** (blocking) in their tmux pane — a `postgres -D ...` or `docker run ...` command occupies the pane's foreground process. `down workspace` reaps the session, terminating each pane's foreground process tree (best-effort).

**This is best-effort.** A service that double-forks, daemonizes, or detaches from the pane's process group before `down` is called may survive the session reap. The orchestrator does not track child PIDs, issue container stop commands, or perform managed/graceful shutdown — managed graceful shutdown is not supported. For workloads where graceful teardown matters (e.g. `docker compose down` instead of `docker compose up` kill), run the teardown logic inside the pane's start command (e.g. a wrapper script that traps SIGHUP and calls `docker compose down` before exiting).
