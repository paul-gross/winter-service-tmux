# Winter service orchestration via tmux — hub

## Feature environment setup steps

After `winter ws init` clones the extension, walk the user through [workflow-setup.md](./workflow-setup.md) to author `workspace:/.winter/config/winter-service-tmux/config.toml` and its companion `layout-hook.sh` — the manifest every feature environment's `./up` requires.

## Reference

| Topic | Read when… |
|-------|------------|
| [Service management rules](./service-rules.md) | …you need the operating rules: `./up`/`./down`/`./restart`/`./status` conventions, log commands and examples, log modes (`file`/`pane`/`memory`) |
| [Workspace-scoped singleton services](./workspace-singletons.md) | …you need to drive or declare shared workspace services (`winter service … workspace`, `scope = "workspace"`) |
| [Log capture configuration](./log-capture.md) | …you need to tune log rotation, understand mixed-mode output, or configure the `[logs]` table |
| [Orchestrator dev loop](./orchestrator-dev-loop.md) | …you are developing the orchestrator and need to test in-progress code against a live worktree |
| [README.md](../README.md) | …installing the extension or binding the `service` capability slot in `.winter/config.toml` |
