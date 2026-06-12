#!/usr/bin/env bash
# winter-service-tmux entrypoint — shared config-file resolution for the
# extension's scripts (up / down / status / doctor.sh / the destroy hook).
# Sourced, never executed. The user authors setup-tmux.sh (+ optional
# setup-tmux.local.sh); this file decides which of them to load and in what
# order.
#
# The project's tmux config lives under workspace:/ai/project/. Two files are
# consulted, each independently preferring the current name and falling back to
# the legacy name kept for backwards compatibility:
#
#   committed:  setup-tmux.sh        (legacy: workflow.sh)
#   local:      setup-tmux.local.sh  (legacy: workflow.local.sh)
#
# Both files are optional, but at least one must exist for there to be a config.
# When both the committed and local files are present they are sourced in order
# — committed first, then local overlaid on top — so the local file can override
# SESSION_PREFIX / ENV_FILE, redefine setup_tmux / status_header, or extend
# WINTER_TMUX_SERVICE_NAMES without being committed to source. This mirrors the
# workspace's config.toml / config.local.toml overlay convention.
#
# Usage:
#   source "<dir>/winter-service-tmux.sh"
#   winter_tmux_config_files "$WORKSPACE_DIR"   # populates WINTER_TMUX_CONFIG_FILES[]
#   for cfg in "${WINTER_TMUX_CONFIG_FILES[@]}"; do source "$cfg"; done
#
# WINTER_TMUX_CONFIG_FILES is set to the ordered list of files to source — 0, 1,
# or 2 entries. An empty array means no config exists at all; callers decide
# whether that is an error (up / status) or a no-op (down / destroy).

# winter_tmux_config_files <workspace-dir>
winter_tmux_config_files() {
  local project_dir="$1/ai/project"
  WINTER_TMUX_CONFIG_FILES=()

  local committed=""
  if [[ -f "$project_dir/setup-tmux.sh" ]]; then
    committed="$project_dir/setup-tmux.sh"
  elif [[ -f "$project_dir/workflow.sh" ]]; then
    committed="$project_dir/workflow.sh"
  fi
  [[ -n "$committed" ]] && WINTER_TMUX_CONFIG_FILES+=("$committed")

  local overlay=""
  if [[ -f "$project_dir/setup-tmux.local.sh" ]]; then
    overlay="$project_dir/setup-tmux.local.sh"
  elif [[ -f "$project_dir/workflow.local.sh" ]]; then
    overlay="$project_dir/workflow.local.sh"
  fi
  [[ -n "$overlay" ]] && WINTER_TMUX_CONFIG_FILES+=("$overlay")

  # Return 0 regardless of the final test above — a missing overlay is normal,
  # and callers run under `set -e`, where a function returning the non-zero
  # status of its last conditional would abort the script.
  return 0
}

# Per-service command store — the single source of truth for what each service
# runs. setup-tmux.sh declares one command per service with winter_service_cmd;
# both setup_tmux (./up) and ./restart resolve a name to its command through
# winter_service_cmd_get, so the two launch paths can't drift. Storage is two
# parallel indexed arrays; authoring is name-keyed.
WINTER_TMUX_CMD_KEYS=()
WINTER_TMUX_CMD_VALS=()

# winter_service_cmd <service-name> <command>
# Declare the command a service runs, keyed by service name. Called from
# setup-tmux.sh, and from a setup-tmux.local.sh overlay to add or override a
# service. Re-declaring a name overrides it — winter_service_cmd_get returns the
# last declaration.
winter_service_cmd() {
  WINTER_TMUX_CMD_KEYS+=("$1")
  WINTER_TMUX_CMD_VALS+=("$2")
}

# winter_service_cmd_get <service-name>
# Print the declared command for a service to stdout and return 0; return 1
# (printing nothing) when the service was never declared. An empty command (a
# bare interactive pane like `shell`) is a declared command — it returns 0 with
# empty output — so callers branch on the exit status, not on whether stdout is
# empty. Returns the last declaration for the name.
winter_service_cmd_get() {
  local name="$1" i found=1 val=""
  for i in "${!WINTER_TMUX_CMD_KEYS[@]}"; do
    if [[ "${WINTER_TMUX_CMD_KEYS[$i]}" == "$name" ]]; then
      val="${WINTER_TMUX_CMD_VALS[$i]}"
      found=0
    fi
  done
  [[ "$found" -eq 0 ]] && printf '%s' "$val"
  return "$found"
}

# winter_tmux_send_service <session> <pane-target> <service-name>
#
# Launch a declared service into an existing tmux pane. This is the single
# source of truth for *how* a service is launched: both setup_tmux (first start,
# driven by the up script) and the restart script call it, so the command a
# service runs can never drift between the two paths.
#
# The command itself comes from the winter_service_cmd store in setup-tmux.sh,
# keyed by service name (the same names declared in WINTER_TMUX_SERVICE_NAMES).
# The launch line is assembled as:
#
#   cd '<worktree>' && [source '<env>' &&] echo '=== <name> ===' [&& <cmd>]
#
# - The leading `cd` resets the pane to the worktree root (read from the
#   exported WINTER_TMUX_WORKTREE_DIR) so the command runs from a deterministic
#   cwd whether the pane was freshly created (up) or its shell has drifted into
#   a subdirectory from a prior run (restart). Relative paths inside <cmd> —
#   e.g. `cd apps/backend && npm run dev` — are thus always relative to the
#   worktree root on every launch.
# - The env-file source is included only when ENV_PATH is set (the up/restart
#   scripts export it after sourcing ENV_FILE).
# - An empty command (e.g. an interactive `shell` pane) yields just the banner,
#   leaving the pane at a prompt.
#
# Returns non-zero without sending anything if the service has no declared
# command, so callers can surface a clear error.
winter_tmux_send_service() {
  local session="$1" pane="$2" name="$3"

  # `local cmd` on its own line: a `local cmd=$(...)` would mask the command
  # substitution's exit status (local always returns 0), so we can't detect an
  # undeclared service. Assign separately and branch on the getter's status.
  local cmd
  if ! cmd="$(winter_service_cmd_get "$name")"; then
    echo "winter-service-tmux: no command declared for service '$name' (declare it with winter_service_cmd in setup-tmux.sh)" >&2
    return 1
  fi

  local prefix=""
  [[ -n "${WINTER_TMUX_WORKTREE_DIR:-}" ]] && prefix="cd '$WINTER_TMUX_WORKTREE_DIR' && "
  [[ -n "${ENV_PATH:-}" ]] && prefix="${prefix}source '$ENV_PATH' && "

  local line="${prefix}echo '=== $name ==='"
  [[ -n "$cmd" ]] && line="$line && $cmd"
  tmux send-keys -t "$session:$pane" "$line" Enter
}
