# Testing changed orchestrator code against a worktree

Because the env-root `./up`/`./down`/`./status`/`./restart` scripts **delegate to `winter service <action> <env>`**, testing changed *provider* (orchestrator) code and testing the *door* (`env_cli`) are two different things. Pick the entry point that reaches the code you changed.

## Provider (orchestrator) code — the common case

The actual `up`/`down`/`status`/`restart`/`logs` orchestration runs through `winter service`, which resolves the **installed** provider. To exercise your in-progress worktree's provider code, point winter at it directly with `--service-orchestrator=` (see `workspace:/context/winter-cli/root-flags.md`):

```bash
winter --service-orchestrator=./alpha/winter-service-tmux service up alpha
winter --service-orchestrator=./alpha/winter-service-tmux service status alpha
```

**Verifying the `status` path requires the feature core too.** `status` env enumeration lives in winter-cli core, not this provider — core computes each scope's environment and injects it on `up`, `down`, and `status`. If your change touches the status path, also point at the feature core with `--winter`:

```bash
winter --winter=./alpha/winter --service-orchestrator=./alpha/winter-service-tmux service status alpha
```

## Door code only — arg parsing and attach

The env-root `./up`/`./down`/`./status`/`./restart` symlinks resolve to the **installed** extension (`winter-service-tmux:/workflow/<script>`), not your in-progress worktree. Set `WINTER_EXT_DIR` to your worktree to run the door from source; the shims prefer `$WINTER_EXT_DIR/src` when set, so no symlink surgery is needed:

```bash
WINTER_EXT_DIR=$PWD/alpha/winter-service-tmux ./alpha/up
```

The door's only in-process work is arg parsing, the `./up -a` attach, and the `down --tmux-only` destroy path — everything else it hands to `winter service`, which still resolves the **installed** provider. So `WINTER_EXT_DIR` exercises door changes (arg-parsing/attach), **not** provider changes; for those, use the `--service-orchestrator=` invocation above.

Pass `WINTER_EXT_DIR` as an inline prefix scoped to that single command — do **not** `export` it. An exported override has no auto-cleanup and silently routes *every* later `./up`/`./down`/`./status`/`./restart` in that shell through worktree door code. The blast radius is only the door (arg-parsing/attach), not orchestration, but an unexpected door override is still confusing — keep it inline. To run the package's unit tests directly, see the repo `CONTRIBUTING.md`.

**Note on `-f` in verification:** `winter service logs … -f` blocks until SIGINT — it never returns on its own. An automated verifier must bound it: `timeout -s INT 10 winter service logs '*/backend' -f`. Alternatively, use the Bash tool's `run_in_background` facility and cancel when done.
