# Testing changed orchestrator code against a worktree

**Verifying the `status` path requires the feature core too.** `status` env enumeration lives in winter-cli core, not this provider — core computes each scope's environment and injects it on `up`, `down`, and `status`. If you are verifying a change that touches the status path, also point at the feature core with `--winter` (see `workspace:/context/winter-cli/root-flags.md`):

```bash
winter --winter=./alpha/winter --service-orchestrator=./alpha/winter-service-tmux service status alpha
```

The env-root `./up`/`./down`/`./status`/`./restart` symlinks resolve to the **installed** extension (`winter-service-tmux:/workflow/<script>`), not your in-progress worktree — so they run committed code unless you override it. Set `WINTER_EXT_DIR` to your in-progress worktree before invoking the env-root script; the shims prefer `$WINTER_EXT_DIR/src` when set, so no symlink surgery or restore is needed:

```bash
WINTER_EXT_DIR=$PWD/alpha/winter-service-tmux ./alpha/up
WINTER_EXT_DIR=$PWD/alpha/winter-service-tmux ./alpha/status
```

Pass `WINTER_EXT_DIR` as an inline prefix on each invocation, scoped to that single command — do **not** `export` it. An exported override has no auto-cleanup and silently routes *every* later `./up`/`./down`/`./status`/`./restart` in that shell through worktree code, reintroducing the footgun the symlink dance had.

The shims (`workflow/up` etc.) are thin Python launchers that call `python3 -m service_orchestrator.env_cli <action>`. To run the package's unit tests directly, see the repo `CONTRIBUTING.md`.

**Note on `-f` in verification:** `winter service logs … -f` blocks until SIGINT — it never returns on its own. An automated verifier must bound it: `timeout -s INT 10 winter service logs '*/backend' -f`. Alternatively, use the Bash tool's `run_in_background` facility and cancel when done.
