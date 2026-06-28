# Winter service orchestration via tmux

Tmux-based service orchestration for winter workspaces. Runs project services (backend, frontend, workers, etc.) in a per-env tmux session via `./up`/`./down`/`./status` scripts (plus `./restart <pattern>...` to bounce one or more services), so multiple envs can run their own instances of the app side-by-side without port conflicts.

## Path notation

Files in this extension are addressed with the `winter-service-tmux:` prefix — for example, `winter-service-tmux:/context/service-rules.md`. Resolve to the on-disk path via the `# Winter Extensions` block in workspace `CLAUDE.md`.

## Feature environment setup steps

This extension needs a project-specific `config.toml` manifest. After `winter ws init` clones the extension, walk the user through [context/workflow-setup.md](./context/workflow-setup.md) to author `workspace:/.winter/config/winter-service-tmux/config.toml` and its companion `layout-hook.sh`. Without these, `./up` errors out in any feature environment.

## Registering the orchestrator

Bind the `service` capability slot to this extension with `[capabilities] service = "winter-service-tmux"` in the workspace `.winter/config.toml` — see [README.md](./README.md) for the full snippet and `workspace:/context/winter-cli/configuration/capabilities.md` for the canonical contract.

## Reference

| Topic | Read when… |
|-------|------------|
| [Service management rules](./context/service-rules.md) | …you need the operating rules: `./up`/`./down`/`./restart`/`./status` conventions, log commands and examples, log modes (`file`/`pane`/`memory`) |
| [Workspace-scoped singleton services](./context/workspace-singletons.md) | …you need to drive or declare shared workspace services (`winter service … workspace`, `scope = "workspace"`) |
| [Log capture configuration](./context/log-capture.md) | …you need to tune log rotation, understand mixed-mode output, or configure the `[logs]` table |
| [Orchestrator dev loop](./context/orchestrator-dev-loop.md) | …you are developing the orchestrator and need to test in-progress code against a live worktree |
