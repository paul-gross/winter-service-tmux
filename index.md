# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-env tmux session via `./up`/`./down`/`./status` scripts, so multiple envs can run their own instances of the app side-by-side without port conflicts.

## Feature environment setup steps

This extension needs a project-specific `workflow.sh` to know which services to run in each feature environment. After `winter ws init` clones the extension, walk the user through [ai/workflow-setup.md](./ai/workflow-setup.md) to generate `workspace:/ai/project/workflow.sh` **and** `workspace:/ai/project/workflow.md` (the agent-facing reference that maps service names to `<window>.<pane>` targets). Without `workflow.sh`, `./up` errors out in any feature environment.

If `workflow.md` is missing, re-run the workflow-setup walkthrough — step 9 generates it. Agents should not reverse-engineer pane indices out of `workflow.sh`.

## Service management rules

Once installed, the workspace conventions are:

- **Never start services as background processes** (no `nohup`, no `&`). Always go through `./up` so they end up in the tmux session.
- **Never kill services directly** (no `kill`, `pkill`, `tmux kill-session`). Always use `./down` so child processes get reaped cleanly.
- Read pane output with `tmux capture-pane`. The per-service `<window>.<pane>` targets and the capture-pane template live in `workspace:/ai/project/workflow.md` — start there to map a service name to its pane target.

Tmux session names are `<SESSION_PREFIX>-<env>` — e.g. `mp-alpha`. The prefix and pane layout are declared in `workspace:/ai/project/workflow.sh`.

## Lifecycle hooks

This extension declares both lifecycle hooks in `winter-ext.toml`:

| Hook | Script | When | What it does |
|------|--------|------|--------------|
| `on_env_init` | `hooks/init-worktree.sh` | After `winter ws init <env>` creates the worktrees and seeds `.winter.env` | Symlinks `./up` / `./down` / `./status` into the env root so they're invokable from `<env>/`. |
| `on_env_destroy` | `hooks/destroy-worktree.sh` | Before `winter ws destroy <env>` removes any per-repo worktree or the env dir | Sources `workspace:/ai/project/workflow.sh` to derive `SESSION_PREFIX`, then runs the extension's own `./down` for the env (falls back to `tmux kill-session` if `./down` is unavailable). Idempotent: exits 0 if the session is already gone, the config is missing, or the config is broken — a syntax error or unbound-variable failure in `workflow.sh` triggers a stderr warning and a best-effort `tmux ls` fallback that matches sessions by `-<env>` suffix, rather than aborting the whole `winter ws destroy`. |

`on_env_destroy` is what makes `winter ws destroy <env>` safe — without it, the env directory would be removed while the tmux session and its child processes were still attached, orphaning everything.
