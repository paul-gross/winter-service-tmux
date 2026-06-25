# Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- **Opt-in startup retry is honored by `winter service up`, not the env-root `./up`.** A `[[service]]` may declare a `[service.startup]` policy (`retries`, `retry_delay`); when a service's process exits during boot, `winter service up` re-launches it up to `retries` times before failing non-zero and naming it (process-exit trigger, not a health probe). The env-root `./up` symlink is a thin no-retry door — same asymmetry as `up --wait`. Schema lives in `winter-service-tmux:/workflow/config.toml.example`.
- **To recover wedged or crashed services, use `./restart <pattern>...`** — the sanctioned alternative to a manual `kill`/`pkill` or a full `./down && ./up`. It reaps each matched service's pane and re-runs its declared command, leaving every other pane in the session running. One or more `<service>` glob patterns are required; they are matched against declared service names scoped to the invoking env (e.g. `./restart backend`, `./restart 'work*'`). The orchestrator expands patterns against its own service catalog — winter forwards them verbatim without expansion.
- **`./status` reports the env it's run from.** `alpha/status` (or `./status` from inside `alpha/`) lists only the `<SESSION_PREFIX>-alpha` session's services; pass `--all` (`./status --all`) for the cross-env view of every running env. Optional `<service>` glob patterns further narrow which services are shown (e.g. `./status back*`). Note: the env-root `./status` and `./restart` doors accept *service-only* glob tokens scoped to the invoking env — not the `<env>/<service>` form used by `winter service status`. Typing `./status alpha/backend` gives a no-match; use `./status backend` instead. Scope comes from *which* env's `./status` you invoke, the same way `./up`/`./down` default to their own env. The two status doors also **render differently by design**: the env-root `./status` prints a human table directly (for a person at a terminal), whereas `winter service status` always emits winter's structured env-keyed JSON status document on stdout and lets winter render it (human table by default, raw JSON under `--json`). The document shape is winter's contract — see `workspace:/ai/winter-cli/usage/service.md` (`status` wire contract); declared `[service.health]` probes populate `health` as `"healthy"` or `"unhealthy"`, and services without probes report `"unknown"`. Health probes are observability only — `./up` does not wait for them — and `cmd` probes run from the service worktree root.
- **Run the scripts from an env dir**, not the workspace root — `alpha/up`, or `cd alpha` first. The scripts are env-root symlinks; there is no `./up`/`./down`/`./status`/`./restart` at the workspace root. (The workspace scope is driven by `winter service … workspace` — see [workspace-singletons.md](./workspace-singletons.md).)
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

  The wire contract and rendering (plain lines vs. NDJSON) are winter's responsibility — see `workspace:/ai/winter-cli/usage/service.md`. There is no env-root `./logs` script; the interface is `winter service logs`. Workspace service logs land at `<workspace-root>/.winter/logs/<service>.log` — `winter service logs workspace` is not wired; read the log files directly.
- **Not all services are captured the same way.** Each `[[service]]` entry has a `log` field (default `"file"`) that controls how its output is captured and read:
  - `"file"` (default): stdout/stderr is captured to `<env>/.winter/logs/<svc>.log` via the capture writer; `logs` reads the persisted file (timestamped, survives `down`). Note: the live pane shows plain (uncolored) output because stdout is piped.
  - `"pane"`: the service is launched bare (TTY preserved); `logs` reads the pane buffer via `tmux capture-pane` on demand (no file persistence, no timestamps, requires a running session). Natural for interactive panes (`shell`) or services where TTY fidelity matters more than persistence.
  - `"memory"`: accepted and validated; not yet implemented — `logs` emits nothing for memory-mode services (future work).
  Services with an empty `cmd` (interactive panes) are always launched bare regardless of `log`.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix is declared as `session_prefix` in `workspace:/.winter/config/winter-service-tmux/config.toml`. A separate `<SESSION_PREFIX>-workspace` session holds workspace-scoped singleton services — see [workspace-singletons.md](./workspace-singletons.md).
