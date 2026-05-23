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

# Tolerate a broken workflow.sh — destroy must remain idempotent even if the
# user is mid-edit or the file has a syntax error. Disable `set -u` across the
# source so an unbound variable inside workflow.sh doesn't abort the hook
# (under -u, `if ! source` is too late: the script exits before the test runs).
SESSION_PREFIX=""
set +u
# shellcheck disable=SC1090
if ! source "$CONFIG" 2>/dev/null; then
  echo "warning: source $CONFIG failed; falling back to env-suffix session lookup" >&2
  SESSION_PREFIX=""
fi
set -u

if [[ -n "$SESSION_PREFIX" ]]; then
  SESSION="${SESSION_PREFIX}-${WINTER_ENV}"
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    exit 0
  fi
else
  # No SESSION_PREFIX — match any tmux session whose name ends with -<env>.
  # If there are none, exit 0 (the idempotency contract).
  SESSION="$(tmux ls -F '#{session_name}' 2>/dev/null | grep -E -- "-${WINTER_ENV}\$" | head -1 || true)"
  if [[ -z "$SESSION" ]]; then
    exit 0
  fi
  echo "warning: using session '$SESSION' inferred from env suffix" >&2
fi

# Prefer the extension's own ./down (handles child-process reaping). Fall back
# to a direct kill-session if ./down can't be located or fails (e.g. ./down
# also sources the broken workflow.sh).
DOWN="$WINTER_EXT_DIR/workflow/down"
if [[ -x "$DOWN" ]]; then
  "$DOWN" "$WINTER_ENV" || tmux kill-session -t "$SESSION" 2>/dev/null || true
else
  tmux kill-session -t "$SESSION" 2>/dev/null || true
fi
