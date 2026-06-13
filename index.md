# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-env tmux session via `./up`/`./down`/`./status` scripts (plus `./restart <service>` to bounce one service), so multiple envs can run their own instances of the app side-by-side without port conflicts.

## Feature environment setup steps

This extension needs a project-specific `setup-tmux.sh` to know which services to run in each feature environment. After `winter ws init` clones the extension, walk the user through [ai/workflow-setup.md](./ai/workflow-setup.md) to author `workspace:/ai/project/setup-tmux.sh`. Without it, `./up` errors out in any feature environment.

`setup-tmux.sh` is the single source of truth. The agent-facing reference `workspace:/ai/project/setup-tmux.md` (which maps service names to `<window>.<pane>` targets) is **generated from it** — never hand-edited. `winter doctor` warns if the two drift, and the workflow-setup walkthrough regenerates it. The generated map reflects committed config only, not the gitignored `setup-tmux.local.sh` overlay.

The committed `setup-tmux.sh` can be paired with a gitignored `setup-tmux.local.sh` for machine-specific overrides — `./up`, `./down`, `./status`, and `./restart` source the local file on top of the committed one. The legacy names `workflow.sh` / `workflow.local.sh` are still honored as fallbacks by those scripts.

Agents should not reverse-engineer pane indices out of `setup-tmux.sh` — read `setup-tmux.md`.

## Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- **To recover a single wedged or crashed service, use `./restart <service>`** — the sanctioned alternative to a manual `kill`/`pkill` or a full `./down && ./up`. It reaps just that service's pane and re-runs its declared command, leaving every other pane in the session running. The argument is a *declared service name*, not an env or worktree (it's env-scoped like `./up`/`./down`/`./status`).
- **`./status` reports the env it's run from.** `alpha/status` (or `./status` from inside `alpha/`) lists only the `<SESSION_PREFIX>-alpha` session's services; pass `--all` (`./status --all`) for the cross-env view of every running env. There is no `<worktree-name>` argument — scope comes from *which* env's `./status` you invoke, the same way `./up`/`./down` default to their own env.
- **Run the scripts from an env dir**, not the workspace root — `alpha/up`, or `cd alpha` first. The scripts are env-root symlinks; there is no `./up`/`./down`/`./status`/`./restart` at the workspace root.
- Read pane output with `tmux capture-pane`. The per-service `<window>.<pane>` targets and the capture-pane template live in `workspace:/ai/project/setup-tmux.md` — start there to map a service name to its pane target.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix and pane layout are declared in `workspace:/ai/project/setup-tmux.sh`.
