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
