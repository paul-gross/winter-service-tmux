#!/usr/bin/env bash
# Symlink the up/down/status scripts into the worktree root.
#
# Invoked by `winter ws init` for every standalone repo whose winter-ext.toml
# declares `[hooks] on_worktree_init`. The CLI runs this script with cwd set
# to the worktree root and provides:
#   WINTER_EXT_DIR        — this extension's repo path
#   WINTER_EXT_PREFIX     — resolved symlink prefix (e.g. "wst")
#   WINTER_WORKSPACE_DIR
#   WINTER_WORKTREE
#   WINTER_WORKTREE_INDEX
#   WINTER_PORT_BASE
set -euo pipefail

: "${WINTER_EXT_DIR:?WINTER_EXT_DIR not set}"

for script in up down status; do
  src="$WINTER_EXT_DIR/workflow/$script"
  if [[ ! -x "$src" ]]; then
    echo "warning: $src missing or not executable" >&2
    continue
  fi
  rel_target="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$src" "$PWD")"
  ln -sfn "$rel_target" "./$script"
done
