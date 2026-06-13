# winter-service-tmux

A [winter](https://github.com/paul-gross/winter) extension that adds tmux-based service orchestration to a workspace. Each feature environment gets `./up`, `./down`, `./status`, and `./restart` scripts that run the project's services in a per-env tmux session, so multiple envs can run their own instances of the app side-by-side without port conflicts.

📚 **Documentation:** <https://paul-gross.github.io/winter-docs/>

## Features

- **Per-env service sessions** — every feature environment gets its own tmux session running the project's services. Humans attach to watch logs in real time while agents drive the lifecycle.
- **Conflict-free parallel runs** — sessions are namespaced per env (`<prefix>-<env>`), so alpha and beta can both run the full app stack at once without colliding.
- **One-command lifecycle** — `./up`, `./down`, `./status`, and `./restart` land in every feature environment on `winter ws init`. Same commands across every project, every env.
- **Single-service restart** — `./restart <service>` reaps one wedged or crashed service's pane and re-runs its declared command, leaving the rest of the session running. No `kill`/`pkill`, no tearing down healthy services. Service commands are declared once with `winter_service_cmd <name> <command>` and shared by both `./up` and `./restart`, so the two never drift.
- **Agent-driven service control** — the `wst-app-runner` agent starts and stops services, reads pane output, and reports health back to the calling agent or session lead.
- **Pluggable project config** — a single `workspace:/ai/project/setup-tmux.sh` declares the panes, commands, and session prefix; the scripts are generic and the project owns the layout. An optional gitignored `setup-tmux.local.sh` overlays machine-specific overrides on top. (The legacy `workflow.sh` / `workflow.local.sh` names still work.)
- **Generated agent map** — the agent-facing `setup-tmux.md` (service name → `<window>.<pane>` target) is rendered from `setup-tmux.sh` by `render-setup-md.sh` and written automatically on every workspace reconcile (`winter ws init`) via the `on_workspace_reconcile` hook. No hand-maintained second source of truth; `winter doctor` flags it if the two fall out of sync.
- **Built-in `winter doctor` probe** — checks tmux is installed, `SESSION_PREFIX` is declared, no foreign tmux sessions collide with the configured prefix, and `setup-tmux.md` is in sync with `setup-tmux.sh`. Surfaces these results under `[wst]` in `winter doctor`'s output.

## Installation & Setup

Agentic setup is hooked into `/ws-setup`.

1. Add to the workspace's `.winter/config.toml`:

   ```toml
   [[standalone_repository]]
   name = "winter-service-tmux"
   url = "git@github.com:paul-gross/winter-service-tmux.git"
   ```

2. Run `/ws-setup` — it walks an interactive guide that generates the project-specific `workspace:/ai/project/setup-tmux.sh` (panes, start commands, session prefix).
3. Commit `setup-tmux.sh` to source — it's the project's service config and belongs in version control. Keep any `setup-tmux.local.sh` overlay gitignored.

See [`index.md`](./index.md) for what this extension contributes and how it plugs into a workspace.
