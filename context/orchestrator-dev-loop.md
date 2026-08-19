# Testing changed orchestrator code against a worktree

Because the env-root `./up`/`./down`/`./status`/`./restart` scripts **delegate to `winter service <action> <env>`**,
testing changed *provider* (orchestrator) code and testing the *door* (`env_cli`) are two different things. Pick the
entry point that reaches the code you changed.

## Provider (orchestrator) code — the common case

The actual `up`/`down`/`status`/`restart`/`logs` orchestration runs through `winter service`, which resolves the
**installed** provider. To exercise your in-progress worktree's provider code, point winter at it directly with
`--service-orchestrator=` (see `workspace:/context/winter-cli/root-flags.md`):

```bash
winter --service-orchestrator=./alpha/winter-service-tmux service up alpha
winter --service-orchestrator=./alpha/winter-service-tmux service status alpha
```

**Verifying the `status` path requires the feature core too.** `status` env enumeration lives in winter-cli core, not
this provider — core computes each scope's environment and injects it on `up`, `down`, and `status`. If your change
touches the status path, also point at the feature core with `--winter`:

```bash
winter --winter=./alpha/winter --service-orchestrator=./alpha/winter-service-tmux service status alpha
```

## Door code only — arg parsing and attach

The env-root `./up`/`./down`/`./status`/`./restart` symlinks resolve to the **installed** extension
(`winter-service-tmux:/workflow/<script>`), not your in-progress worktree. Set `WINTER_EXT_DIR` to your worktree to run
the door from source; the shims prefer `$WINTER_EXT_DIR/src` when set, so no symlink surgery is needed:

```bash
WINTER_EXT_DIR=$PWD/alpha/winter-service-tmux ./alpha/up
```

The door's only in-process work is arg parsing, the `./up -a` attach, and the `down --tmux-only` destroy path —
everything else it hands to `winter service`, which still resolves the **installed** provider. So `WINTER_EXT_DIR`
exercises door changes (arg-parsing/attach), **not** provider changes; for those, use the `--service-orchestrator=`
invocation above.

Pass `WINTER_EXT_DIR` as an inline prefix scoped to that single command — do **not** `export` it. An exported override
has no auto-cleanup and silently routes *every* later `./up`/`./down`/`./status`/`./restart` in that shell through
worktree door code. The blast radius is only the door (arg-parsing/attach), not orchestration, but an unexpected door
override is still confusing — keep it inline. To run the package's unit tests directly, see the repo `CONTRIBUTING.md`.

**Note on `-f` in verification:** `winter service logs … -f` blocks until SIGINT — it never returns on its own. An
automated verifier must bound it: `timeout -s INT 10 winter service logs '*/backend' -f`. Alternatively, use the Bash
tool's `run_in_background` facility and cancel when done.

## Doctor probe

`workflow/doctor.sh` runs as part of `winter doctor`, checking tmux is on PATH, the manifest validates, no foreign tmux
session collides with the resolved session prefix, and the `layout_hook` (when declared) exists and is executable. See
`workspace:/context/winter-cli/configuration/doctor.md` for the doctor-probe contract.

Prefer the automated route first: `tests/test_doctor_probe.py` drives the script end-to-end with a faked `tmux` on
`PATH` — extend it when changing probe logic rather than relying on manual runs alone (see `CONTRIBUTING.md`).

For a manual run, invoke it standalone:

```bash
WINTER_WORKSPACE_DIR=/path/to/workspace WINTER_SERVICE_PREFIX=<prefix> bash workflow/doctor.sh
```

- `WINTER_WORKSPACE_DIR` — the workspace root the session-name-collision probe scans for feature-env worktrees.
  **Coupled trap:** `doctor.sh` falls back to `$(pwd)` when this is unset (`workflow/doctor.sh:20`), so running the
  script from the wrong cwd makes it scan the wrong tree — every one of your own sessions is then misclassified as
  foreign, the identical false-positive class this fix removes. Always set it explicitly for a manual run.
- `WINTER_SERVICE_PREFIX` — the session-name prefix probe 3 compares tmux sessions against (mirrors the var winter
  injects on every dispatched action). A manifest `session_prefix` override, if declared in the resolved `config.toml`,
  takes precedence.
- `WINTER_EXT_CONFIG_DIR` — optional; the extension config dir holding `config.toml`. Falls back to
  `<WINTER_WORKSPACE_DIR>/.winter/config/winter-service-tmux/` when unset.

**Throwaway-session A/B procedure** for the collision probe specifically — create sessions distinctly named so you can't
confuse them with a real env or another agent's session, and kill only those:

```bash
tmux new-session -d -s "<prefix>-workspace"       # A: should classify as own (pass)
tmux new-session -d -s "<prefix>-doctor-probe-zz"  # B: should classify as foreign (warn)
WINTER_WORKSPACE_DIR=/path/to/workspace WINTER_SERVICE_PREFIX=<prefix> bash workflow/doctor.sh
tmux kill-session -t "<prefix>-workspace"
tmux kill-session -t "<prefix>-doctor-probe-zz"
```

Never `tmux kill-session` a session you didn't create for this check — see
`winter-service-tmux:/context/service-rules.md`'s "never kill services directly" rule.
