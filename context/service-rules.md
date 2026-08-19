# Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up`, which starts
  everything the workspace registers.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get
  reaped cleanly.
- **The env-root `./up`/`./down`/`./status`/`./restart` delegate to `winter service`.** They are thin convenience doors
  over `winter service <action> <env>`, so they fan out across *every* bound provider (capability dispatch), not just
  this tmux orchestrator — e.g. in a workspace that also runs a docker provider, `./up` starts the docker services too.
  The one thing the tmux door adds on top is `./up -a`/`--attach`, which execs `tmux attach-session` after a successful
  up (attach is inherently a tmux concern — `winter service` has no `-a`). Because the doors delegate, they inherit
  `winter service` behavior: `./up` ensures the workspace scope is up first and honors the `[service.startup]` retry
  policy (below). The one `winter service up` capability the door does *not* forward is `--wait`; run
  `winter service up <env> --wait` directly for the readiness gate.
- **Opt-in startup retry is honored on `up`.** A `[[service]]` may declare a `[service.startup]` policy (`retries`,
  `retry_delay`); when a service's process exits during boot, `up` re-launches it up to `retries` times before failing
  non-zero and naming it (process-exit trigger, not a health probe). Since `./up` delegates to `winter service up`, the
  env-root door honors this policy too. Schema lives in `winter-service-tmux:/workflow/config.toml.example`.
- **Opt-in `depends_on` gates launch ordering on `up`.** A `[[service]]` may declare `depends_on = [...]` — one or more
  service patterns that must be ready (healthy, or running when no probe is declared) before this service's pane is
  launched; same-scope dependencies are also launched in dependency order regardless of declaration order. Each
  dependency is polled for up to the caller's effective `--wait --timeout` — winter core injects the resolved value as
  `WINTER_SERVICE_TIMEOUT` on `up`, falling back to 120 seconds when the var is absent, empty, non-numeric, or
  non-positive (e.g. an older core that never injects it); if the dependency never becomes ready within that window,
  `up` exits non-zero naming both the waiting service and the unmet dependency (the same class of up-time failure
  `[service.startup]` retry produces, above, but a dependency-readiness trigger rather than a process-exit one). Since
  `./up` delegates to `winter service up`, the env-root door honors this too. Schema lives in
  `winter-service-tmux:/workflow/config.toml.example`.
- **`up`/`down` accept a `<scope>/<svc-pattern>` for a partial launch/reap**, a peer to the
  `./restart <pattern>`/`./status <glob>` glob support below: `winter service up alpha/backend` launches only the
  matched service(s), leaving every other declared service untouched; `winter service down alpha/backend` reaps only the
  matched service(s)' pane children and leaves the session running. A pattern that matches no declared service prints
  `orchestrate: <action>: pattern '<scope>/<svc-pattern>' matched no services` and leaves the session completely
  untouched — no pane is created, reaped, or killed. The env-root `./up`/`./down` doors do **not** forward a service
  glob (they always delegate a bare `winter service up/down <env>`, whole-scope); reach partial up/down via
  `winter service up`/`down <env>/<svc-pattern>` directly.
- **To recover wedged or crashed services, use `./restart <pattern>...`** — the sanctioned alternative to a manual
  `kill`/`pkill` or a full `./down && ./up`. It restarts each matched service (for a tmux service, reaping its pane and
  re-running its declared command), leaving every other service running. One or more `<service>` glob patterns are
  required, matched against service names scoped to the invoking env (e.g. `./restart backend`, `./restart 'work*'`).
  The env-root door prefixes each pattern with the invoking env (`backend` → `alpha/backend`) and delegates to
  `winter service restart`, which routes each matched service to its **owning** provider — so `./restart db` bounces a
  docker-backed service just as `./restart backend` bounces a tmux one.
- **`./status` reports the env it's run from.** `alpha/status` (or `./status` from inside `alpha/`) lists only env
  `alpha`'s services — every provider's, not just the tmux `<SESSION_PREFIX>-alpha` session's — and no other env; pass
  `--all` (`./status --all`) for the cross-env view of every running env. Optional `<service>` glob patterns further
  narrow which services are shown (e.g. `./status back*`). Note: the env-root `./status` and `./restart` doors accept
  *service-only* glob tokens scoped to the invoking env — not the `<env>/<service>` form used by
  `winter service status`. Typing `./status alpha/backend` gives a no-match; use `./status backend` instead. Scope comes
  from *which* env's `./status` you invoke, the same way `./up`/`./down` default to their own env. Because `./status`
  now delegates to `winter service status <env>`, the two render **identically** — winter renders the structured
  env-keyed status document (human table by default, raw JSON under `winter service status --json`), and with a docker
  provider also bound the table merges both providers' services. The document shape is winter's contract — see
  `workspace:/context/winter-cli/usage/service.md` (`status` wire contract); declared `[service.health]` probes populate
  `health` as `"healthy"` or `"unhealthy"`, and services without probes report `"unknown"`. Health probes are
  observability only — `./up` does not wait for them — and `cmd` probes run from the service worktree root. A `log`-type
  probe treats `target` as a regular expression matched (`re.search`) against the service's captured output — the live
  tmux pane buffer for `log = "pane"` services, or a bounded tail of the captured log file for `log = "file"` services
  (also accepted, forward-compat only, for `log = "memory"` — not yet implemented, so there is no captured file to read
  yet) — and, unlike `url`/`cmd`, `target` is used verbatim (no `${VAR}` interpolation), so regex syntax is never
  mangled; `winter service up <env> --wait` polls this same status document, so it blocks until a declared `log` pattern
  matches (or the wait times out). An `uptime`-type probe treats `target` as a duration (`<N><unit>`, unit one of
  s/m/h/d — the same form `winter service logs --since` accepts) and passes once the service's measured process — its
  tmux pane's direct child, never the pane shell, the longest-running one when several exist — has been alive at least
  that long; it is the dumb, last-resort readiness signal for a service with no HTTP endpoint, cheap readiness command,
  or distinctive ready-line, and is unhealthy whenever the pane has no measurable child (an interactive pane, or the
  process exited); because the clock is anchored on the child process, `./restart <svc>` naturally resets it, and
  `winter service up <env> --wait` blocks roughly the declared duration then passes the same way it does for the other
  probe types. Services that declare a `port` field surface the resolved port in the `ports` field of the status
  document / `PORTS` column of the rendered table; the orchestrator resolves `WINTER_PORT_BASE + <offset>` expressions
  against `WINTER_PORT_BASE` at status time — see the env-injection note below. Services without a declared `port` emit
  an empty `ports` field.
- **Run the scripts from an env dir**, not the workspace root — `alpha/up`, or `cd alpha` first. The scripts are
  env-root symlinks; there is no `./up`/`./down`/`./status`/`./restart` at the workspace root. (The workspace scope is
  driven by `winter service … workspace` — see [workspace-singletons.md](./workspace-singletons.md).)
- **To read service output, use `winter service logs <env>`** (preferred) — output is captured to
  `<env>/.winter/logs/<service>.log` on `up` and persists across restarts, `down`, and teardown. Logs are timestamped
  and searchable. Examples:

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

  The wire contract and rendering (plain lines vs. NDJSON) are winter's responsibility — see
  `workspace:/context/winter-cli/usage/service.md`. There is no env-root `./logs` script; the interface is
  `winter service logs`. Workspace service logs land at `<workspace-root>/.winter/logs/<service>.log` —
  `winter service logs workspace` is not wired; read the log files directly.
- **A `[[service]]` may declare a `port` field** — either a literal integer (`port = 8080`) or an env-relative offset
  expression (`port = "WINTER_PORT_BASE + 10"`). A bare integer is an absolute port number (the same in every
  environment); the `WINTER_PORT_BASE + <offset>` form is resolved per-env at status time against the `WINTER_PORT_BASE`
  that core injects into the provider's process environment (see the env-injection note below). Declared ports are
  surfaced in the `ports` field of winter's status document and the `PORTS` column of the rendered table — see
  `workspace:/context/winter-cli/usage/service.md` for the wire shape. Services without `port` emit an empty `ports`
  field.
- **Env-injection contract:** Core (winter-cli) computes and injects the scope's full environment into the provider
  subprocess. The injected variable list and the per-action injection matrix (which actions receive the env, and which
  do not) are owned by `workspace:/context/winter-cli/contracts/service-orchestrator.md`. This provider reads those vars
  from `os.environ` and does **not** locate, open, or shell-source any `.winter.env` file. There is no
  `WINTER_INJECTED_KEYS`. Tmux panes are children of the tmux server, not of the provider process, so they do not
  inherit the process environment automatically. Instead, each pane's launch line self-sources via
  `eval "$(winter env <scope>)"` before running its command — this brings `WINTER_PORT_BASE` and all declared env-var
  band entries into the pane shell regardless of tmux session lifecycle. When the manifest declares an `env_file`, that
  file is additionally dot-sourced (`&& . '<env_file>'`) after the scope eval, layering machine-specific credentials on
  top of winter's vars. **PATH requirement:** the `winter` CLI must be on the PATH of the tmux pane's shell (a non-login
  shell by default) since each pane self-sources `eval "$(winter env <scope>)"` unguarded. If `winter` is absent, the
  pane prints `winter: command not found` and the service starts without its scope env. This is normally satisfied in a
  winter workspace where the PATH is configured in the shell's rc file.
- **Not all services are captured the same way.** Each `[[service]]` entry has a `log` field (default `"file"`) that
  controls how its output is captured and read:
  - `"file"` (default): stdout/stderr is captured to `<env>/.winter/logs/<svc>.log` via the capture writer; `logs` reads
    the persisted file (timestamped, survives `down`). Note: the live pane shows plain (uncolored) output because stdout
    is piped.
  - `"pane"`: the service is launched bare (TTY preserved); `logs` reads the pane buffer via `tmux capture-pane` on
    demand (no file persistence, no timestamps, requires a running session). Natural for interactive panes (`shell`) or
    services where TTY fidelity matters more than persistence.
  - `"memory"`: accepted and validated; not yet implemented — `logs` emits nothing for memory-mode services (future
    work). Services with an empty `cmd` (interactive panes) are always launched bare regardless of `log`.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix is controlled entirely by winter: it's
resolved from the `WINTER_SERVICE_PREFIX` environment variable, a base extension var that winter injects into the
provider process on every dispatched action (`up`/`down`/`status`/`restart`/`logs` — see
`workspace:/context/winter-cli/contracts/service-orchestrator.md`) — the workspace-level prefix configured once for the
whole workspace, not per-provider. A deprecated manifest `session_prefix` key in
`workspace:/.winter/config/winter-service-tmux/config.toml` is an explicit per-provider override that takes precedence
when set — checked first, used if declared — which happens to also cover the residual case where `WINTER_SERVICE_PREFIX`
is somehow absent (e.g. this provider's CLI invoked directly outside `winter service` dispatch with a stripped
environment); it is not part of normal setup, and since the env var is a base extension var present on every action,
`winter service restart`/`logs` do not need it in the common case. A separate `<SESSION_PREFIX>-workspace` session holds
workspace-scoped singleton services — see [workspace-singletons.md](./workspace-singletons.md).
