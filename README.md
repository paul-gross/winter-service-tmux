# winter-service-tmux

A [winter](https://codeberg.org/pgross/winter) extension that adds tmux-based service orchestration to a workspace. Each feature worktree gets `./up`, `./down`, and `./status` scripts that run the project's services in a per-worktree tmux session, so multiple worktrees can run their own instances of the app side-by-side without port conflicts.

## Features

- **Per-worktree service sessions** — every feature worktree gets its own tmux session running the project's services. Humans attach to watch logs in real time while agents drive the lifecycle.
- **Conflict-free parallel runs** — sessions are namespaced per worktree (`<prefix>-<worktree>`), so alpha and beta can both run the full app stack at once without colliding.
- **One-command lifecycle** — `./up`, `./down`, and `./status` land in every feature worktree on `winter ws init`. Same three commands across every project, every worktree.
- **Agent-driven service control** — the `wst-app-runner` agent starts and stops services, reads pane output, and reports health back to the calling agent or session lead.
- **Pluggable project config** — a single `workspace:/ai/project/workflow.sh` declares the panes, commands, and session prefix; the scripts are generic and the project owns the layout.
- **Built-in `winter doctor` probe** — checks tmux is installed, `SESSION_PREFIX` is declared, and no foreign tmux sessions collide with the configured prefix. Surfaces three results under `[wst]` in `winter doctor`'s output.

## Installation & Setup

Agentic setup is hooked into `/ws-setup`.

1. Add to the workspace's `.winter/config.toml`:

   ```toml
   [[standalone_repository]]
   name = "winter-service-tmux"
   url = "git@codeberg.org:pgross/winter-service-tmux.git"
   ```

2. Run `/ws-setup` — it walks an interactive guide that generates the project-specific `workspace:/ai/project/workflow.sh` (panes, start commands, session prefix).
3. Commit `workflow.sh` to source — it's the project's service config and belongs in version control.

See [`index.md`](./index.md) for what this extension contributes and how it plugs into a workspace.
