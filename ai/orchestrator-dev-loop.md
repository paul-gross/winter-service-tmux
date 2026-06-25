# Testing changed orchestrator code against a worktree

The env-root `./up`/`./down`/`./status`/`./restart` symlinks resolve to the **installed** extension (`winter-service-tmux:/workflow/<script>`), not your in-progress worktree — so they run committed code until you repoint them. To exercise changed orchestrator code, override the symlink at the worktree's copy, run the real entrypoint, then restore it (using `alpha` as the example env):

```bash
readlink alpha/up                                          # record original: ../.winter/ext/service-tmux/workflow/up
ln -sfn winter-service-tmux/workflow/up alpha/up           # override -> alpha/winter-service-tmux/workflow/up (sibling-relative)
cd alpha && ./up && ./status                               # exercise via the real entrypoint
ln -sfn ../.winter/ext/service-tmux/workflow/up alpha/up   # restore — always, even if the test failed
```

Repeat per script you changed. **Restore is mandatory** — a left-over override silently makes every later service call in that env run worktree code.

The shims (`workflow/up` etc.) are thin Python launchers that call `python3 -m service_orchestrator.env_cli <action>`. To run the package's unit tests directly, see the repo `CONTRIBUTING.md`.

**Note on `-f` in verification:** `winter service logs … -f` blocks until SIGINT — it never returns on its own. An automated verifier must bound it: `timeout -s INT 10 winter service logs '*/backend' -f`. Alternatively, use the Bash tool's `run_in_background` facility and cancel when done.
