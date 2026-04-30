# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-worktree tmux session via `./up`/`./down`/`./status` scripts, so multiple worktrees can run their own instances of the app side-by-side without port conflicts.

## Feature environment setup steps

This extension needs a project-specific `workflow.sh` to know which services to run in each feature environment. After `winter ws init` clones the extension, walk the user through [ai/workflow-setup.md](./ai/workflow-setup.md) to generate `workspace:/ai/project/workflow.sh`. Without that file, `./up` errors out in any feature environment.

## Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- Read pane output with `tmux capture-pane -t <prefix>-<worktree>:0.<pane> -p | tail -20`.

Tmux session names are `<SESSION_PREFIX>-<worktree>` — e.g. `mp-alpha`. The prefix and pane layout come from `workspace:/ai/project/workflow.sh`.
