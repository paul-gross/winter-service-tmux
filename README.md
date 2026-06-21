# ❄️ winter-service-tmux

A [winter](https://github.com/paul-gross/winter) extension that adds tmux-based service orchestration to a workspace. Each feature environment gets `./up`, `./down`, `./status`, and `./restart` scripts that run the project's services in a per-env tmux session, so multiple envs can run their own instances of the app side-by-side without port conflicts.

📚 **Documentation:** <https://paul-gross.github.io/winter-docs/>

## ✨ Features

- **Per-env service sessions** — every feature environment gets its own tmux session running the project's services. Humans attach to watch logs in real time while agents drive the lifecycle.
- **Conflict-free parallel runs** — sessions are namespaced per env (`<prefix>-<env>`), so alpha and beta can both run the full app stack at once without colliding.
- **One-command lifecycle** — `./up`, `./down`, `./status`, and `./restart` land in every feature environment on `winter ws init`. Same commands across every project, every env.
- **Selective restart** — `./restart <pattern>...` reaps each matched service's pane and re-runs its declared command, leaving the rest of the session running. One or more `<service>` glob patterns (e.g. `backend`, `work*`) are matched against declared service names scoped to the invoking env. No `kill`/`pkill`, no tearing down healthy services. Service commands are declared once in `[[service]]` entries in `config.toml` and shared by both `./up` and `./restart`, so the two never drift. `./status` accepts the same `<pattern>...` service filtering to narrow which services are shown (e.g. `./status back*`).
- **Persistent log capture** — every commanded service's stdout/stderr is piped through a capture writer and written to `<env>/.winter/logs/<service>.log` (timestamped, size-rotated). Logs survive `down` and teardown; read them with `winter service logs <pattern>... [-f] [-n N] [--since TS] [--until TS] [-t] …` where patterns are `<env>/<service>` segment-globs (e.g. `alpha/backend`, `'*/backend'`, `alpha`). `-f` follows ALL matched `(env, service)` streams concurrently, interleaving their lines into one output until Ctrl-C — e.g. `winter service logs alpha -f` tails every service in an env, and `winter service logs '*/backend' -f` tails backend across every running env.
- **Agent-driven service control** — the `app-runner` agent starts and stops services, reads logs, and reports health back to the calling agent or session lead.
- **Declarative TOML manifest** — a single `workspace:/.winter/config/winter-service-tmux/config.toml` declares the panes, commands, session prefix, and status URLs; the orchestrator is generic and the project owns the layout. An optional gitignored `config.local.toml` in the same directory overlays per-machine overrides on top.
- **Python orchestrator** — a stdlib-only Python package (`src/service_orchestrator/`) implements the full `up`/`down`/`status`/`restart` lifecycle, consuming the TOML manifest via the `service_manifest` reader. No bash runtime dependency for service management.
- **Capability-registry integration** — exposes the orchestrator to winter's `winter service <action> <env>` dispatch via the `service` capability slot. Bind it in `.winter/config.toml` with `[capabilities] service = "winter-service-tmux"` and declare the entrypoint in this extension's `winter-ext.toml` with `[provides] service = "workflow/orchestrate"`. The legacy keys `service_orchestrator` (config) and `orchestrate_services` (manifest) are still accepted as deprecated aliases.
- **Built-in `winter doctor` probe** — checks tmux is installed, `session_prefix` is declared in the manifest, no foreign tmux sessions collide with the configured prefix, and the manifest validates cleanly. Surfaces these results under `[wst]` in `winter doctor`'s output.

## 🚀 Installation & Setup

Agentic setup is hooked into `/ws-setup`.

1. Add to the workspace's `.winter/config.toml`. Use the `[capabilities]` table to bind the `service` slot to this extension, and add a `[[standalone_repository]]` entry to install it:

   ```toml
   [capabilities]
   service = "winter-service-tmux"

   [[standalone_repository]]
   name = "winter-service-tmux"
   url = "git@github.com:paul-gross/winter-service-tmux.git"
   path = ".winter/ext/service-tmux"
   ```

   The legacy root-level key `service_orchestrator = "winter-service-tmux"` is still accepted as a deprecated alias and is automatically folded into `capabilities.service` at config load, but `[capabilities]` is the supported form for new workspaces.

   See `workflow/config.toml.example` and `workflow/config.local.toml.example` for the full manifest schema.

2. Run `/ws-setup` — it walks an interactive guide that generates the project-specific `workspace:/.winter/config/winter-service-tmux/config.toml` (services, pane targets, session prefix) and `layout-hook.sh` in the same directory (tmux window/pane geometry).
3. Commit `config.toml` and `layout-hook.sh` to source — they're the project's service config and belong in version control. Keep any `config.local.toml` overlay gitignored.

See [`index.md`](./index.md) for what this extension contributes and how it plugs into a workspace.

## License

MIT.
