# Winter service orchestration via tmux

Implements winter's `service` capability slot with tmux: runs each env's project services (backend, frontend, workers, …) in a per-env tmux session via `./up`/`./down`/`./status` scripts (plus `./restart <pattern>...`), so multiple envs run their own instances of the app side-by-side without port conflicts.

Read [context/index.md](./context/index.md) when setting up, operating, or developing this orchestrator — the hub for its setup steps, operating rules, and reference.
