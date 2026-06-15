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
# `winter ws destroy`), exits 0 without complaint. The Python `down` door
# has its own env-suffix fallback when the manifest is unreadable, so this
# hook never gets stuck.
set -uo pipefail

: "${WINTER_EXT_DIR:?WINTER_EXT_DIR not set}"
: "${WINTER_ENV:?WINTER_ENV not set}"

# Invoke the extension's `down` shim for this env. The Python env_cli door
# handles the manifest-unreadable case via env-suffix session matching
# (resolved decision #2), so no bash-level fallback is needed here.
DOWN="$WINTER_EXT_DIR/workflow/down"
if [[ -x "$DOWN" ]]; then
  "$DOWN" "$WINTER_ENV" || true
fi
