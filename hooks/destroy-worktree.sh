#!/usr/bin/env bash
# Stop the tmux session for this feature env before its files are removed.
#
# Invoked by `winter ws destroy` for every standalone repo whose
# winter-ext.toml declares `[hooks] on_env_destroy`. The CLI runs this script
# with cwd set to the env root (the dir about to be torn down) and provides
# the same env-var contract as on_env_init:
#   WINTER_EXT_DIR        — this extension's repo path
#   WINTER_EXT_PREFIX     — resolved symlink prefix (e.g. "wst")
#   WINTER_WORKSPACE_DIR
#   WINTER_ENV
#   WINTER_ENV_INDEX
#   WINTER_PORT_BASE
#
# Idempotent: if the session is already gone (e.g. user ran `./down` before
# `winter ws destroy`), exits 0 without complaint.
set -uo pipefail

: "${WINTER_EXT_DIR:?WINTER_EXT_DIR not set}"
: "${WINTER_WORKSPACE_DIR:?WINTER_WORKSPACE_DIR not set}"
: "${WINTER_ENV:?WINTER_ENV not set}"

CONFIG="$WINTER_WORKSPACE_DIR/ai/project/workflow.sh"
if [[ ! -f "$CONFIG" ]]; then
  # No workflow.sh = no services to stop. Not an error during destroy.
  exit 0
fi

# shellcheck disable=SC1090
source "$CONFIG"
: "${SESSION_PREFIX:?workflow.sh must define SESSION_PREFIX}"

SESSION="${SESSION_PREFIX}-${WINTER_ENV}"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  exit 0
fi

# Prefer the extension's own ./down (handles child-process reaping). Fall back
# to a direct kill-session if ./down can't be located.
DOWN="$WINTER_EXT_DIR/workflow/down"
if [[ -x "$DOWN" ]]; then
  "$DOWN" "$WINTER_ENV" || tmux kill-session -t "$SESSION" 2>/dev/null || true
else
  tmux kill-session -t "$SESSION" 2>/dev/null || true
fi
