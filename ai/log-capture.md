# Log capture configuration

File-mode log output lands at `<env>/.winter/logs/<service>.log`, persists across `down` and teardown, and is size-rotated. Log behavior is configured via the `[logs]` table in `workspace:/.winter/config/winter-service-tmux/config.toml`; per-machine overrides go in the gitignored `config.local.toml` in the same directory. The full key/default table and overlay semantics are documented in `winter-service-tmux:/workflow/config.toml.example` and `winter-service-tmux:/workflow/config.local.toml.example`.

**Note on mixed-mode output:** pane-mode events carry no timestamp and sort before file-mode events in the merged stream. When `-n N` spans both file and pane services, N is an approximation across the mixed set.
