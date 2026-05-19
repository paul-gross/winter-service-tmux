# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-worktree tmux session via `./up`/`./down`/`./status` scripts, so multiple worktrees can run their own instances of the app side-by-side without port conflicts.

## Feature environment setup steps

This extension needs a project-specific `workflow.sh` to know which services to run in each feature environment. After `winter ws init` clones the extension, walk the user through [ai/workflow-setup.md](./ai/workflow-setup.md) to generate `workspace:/ai/project/workflow.sh`. Without that file, `./up` errors out in any feature environment.

## Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- Read pane output with `tmux capture-pane`. The per-service `<window>.<pane>` targets and the capture-pane template live in `workspace:/ai/project/workflow.md` — start there to map a service name to its pane target.

Tmux session names are `<SESSION_PREFIX>-<worktree>` — e.g. `mp-alpha`. The prefix and pane layout are declared in `workspace:/ai/project/workflow.sh`.
