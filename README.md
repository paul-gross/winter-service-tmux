# winter-service-tmux

A [winter](https://github.com/paul-gross/winter) extension that adds tmux-based service orchestration to a workspace. Each feature environment gets `./up`, `./down`, `./status`, and `./restart` scripts that run the project's services in a per-env tmux session, so multiple envs can run their own instances of the app side-by-side without port conflicts.

📚 **Documentation:** <https://paul-gross.github.io/winter-docs/>

## Features

- **Per-env service sessions** — every feature environment gets its own tmux session running the project's services. Humans attach to watch logs in real time while agents drive the lifecycle.
- **Conflict-free parallel runs** — sessions are namespaced per env (`<prefix>-<env>`), so alpha and beta can both run the full app stack at once without colliding.
- **One-command lifecycle** — `./up`, `./down`, `./status`, and `./restart` land in every feature environment on `winter ws init`. Same commands across every project, every env.
- **Single-service restart** — `./restart <service>` reaps one wedged or crashed service's pane and re-runs its declared command, leaving the rest of the session running. No `kill`/`pkill`, no tearing down healthy services. Service commands are declared once in `[[service]]` entries in `setup-tmux.toml` and shared by both `./up` and `./restart`, so the two never drift.
- **Agent-driven service control** — the `wst-app-runner` agent starts and stops services, reads pane output, and reports health back to the calling agent or session lead.
- **Declarative TOML manifest** — a single `workspace:/ai/project/setup-tmux.toml` declares the panes, commands, session prefix, and status URLs; the orchestrator is generic and the project owns the layout. An optional gitignored `setup-tmux.local.toml` overlays per-machine overrides on top.
- **Python orchestrator** — a stdlib-only Python package (`src/service_orchestrator/`) implements the full `up`/`down`/`status`/`restart` lifecycle, consuming the TOML manifest via the `service_manifest` reader. No bash runtime dependency for service management.
- **`orchestrate_services` entrypoint** — exposes the orchestrator to winter's `winter service <action> <env>` dispatch. Register the extension in `.winter/config.toml` with `service_orchestrator = "winter-service-tmux"`.
- **Built-in `winter doctor` probe** — checks tmux is installed, `session_prefix` is declared in the manifest, no foreign tmux sessions collide with the configured prefix, and the manifest validates cleanly. Surfaces these results under `[wst]` in `winter doctor`'s output.

## Installation & Setup

Agentic setup is hooked into `/ws-setup`.

1. Add to the workspace's `.winter/config.toml`. `service_orchestrator` is a **root-level** key — place it at the top of the file, before any `[[standalone_repository]]` tables, so TOML binds it correctly:

   ```toml
   # top-level key — must appear before any [[table]] headers
   service_orchestrator = "winter-service-tmux"

   [[standalone_repository]]
   name = "winter-service-tmux"
   url = "git@github.com:paul-gross/winter-service-tmux.git"
   path = ".winter/ext/service-tmux"
   ```

   See `workflow/setup-tmux.toml.example` and `workflow/setup-tmux.local.toml.example` for the full manifest schema.

2. Run `/ws-setup` — it walks an interactive guide that generates the project-specific `workspace:/ai/project/setup-tmux.toml` (services, pane targets, session prefix) and `workspace:/ai/project/layout-hook.sh` (tmux window/pane geometry).
3. Commit `setup-tmux.toml` and `layout-hook.sh` to source — they're the project's service config and belong in version control. Keep any `setup-tmux.local.toml` overlay gitignored.

See [`index.md`](./index.md) for what this extension contributes and how it plugs into a workspace.
