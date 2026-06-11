# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-env tmux session via `./up`/`./down`/`./status` scripts, so multiple envs can run their own instances of the app side-by-side without port conflicts.

## Feature environment setup steps

This extension needs a project-specific `setup-tmux.sh` to know which services to run in each feature environment. After `winter ws init` clones the extension, walk the user through [ai/workflow-setup.md](./ai/workflow-setup.md) to generate `workspace:/ai/project/setup-tmux.sh` **and** `workspace:/ai/project/setup-tmux.md` (the agent-facing reference that maps service names to `<window>.<pane>` targets). Without it, `./up` errors out in any feature environment.

The committed `setup-tmux.sh` can be paired with a gitignored `setup-tmux.local.sh` for machine-specific overrides — `./up`, `./down`, and `./status` source the local file on top of the committed one. The legacy names `workflow.sh` / `workflow.local.sh` (and the agent reference `workflow.md`) are still honored as fallbacks.

If `setup-tmux.md` is missing, re-run the [workflow-setup walkthrough](./ai/workflow-setup.md) to regenerate it. Agents should not reverse-engineer pane indices out of `setup-tmux.sh`.

## Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- **`./status` reports the env it's run from.** `alpha/status` (or `./status` from inside `alpha/`) lists only the `<SESSION_PREFIX>-alpha` session's services; pass `--all` (`./status --all`) for the cross-env view of every running env. There is no `<worktree-name>` argument — scope comes from *which* env's `./status` you invoke, the same way `./up`/`./down` default to their own env.
- **Run the scripts from an env dir**, not the workspace root — `alpha/up`, or `cd alpha` first. The scripts are env-root symlinks; there is no `./up`/`./down`/`./status` at the workspace root.
- Read pane output with `tmux capture-pane`. The per-service `<window>.<pane>` targets and the capture-pane template live in `workspace:/ai/project/setup-tmux.md` — start there to map a service name to its pane target.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix and pane layout are declared in `workspace:/ai/project/setup-tmux.sh`.
