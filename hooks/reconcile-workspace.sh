#!/usr/bin/env bash
# Regenerate the agent-facing setup-tmux.md from the committed setup-tmux.sh.
#
# Invoked by `winter ws init` (workspace-level reconcile) for standalone repos
# whose winter-ext.toml declares `[hooks] on_workspace_reconcile`. The CLI runs
# this script with cwd at the workspace root and provides:
#   WINTER_WORKSPACE_DIR  — absolute path to the workspace root
#   WINTER_EXT_DIR        — this extension's repo path
#   WINTER_EXT_PREFIX     — resolved symlink prefix (e.g. "wst")
#
# No env-scoped vars (WINTER_ENV / WINTER_ENV_INDEX / WINTER_PORT_BASE) are
# provided — this hook is workspace-level, not per-env.
#
# Behaviour:
#   - Renders setup-tmux.md by running render-setup-md.sh and writing its stdout
#     to $WINTER_WORKSPACE_DIR/ai/project/setup-tmux.md.
#   - Writes via a temp file so a render error never truncates an existing good
#     setup-tmux.md.
#   - Exit 2 from render-setup-md.sh means no committed setup-tmux.sh exists:
#     treated as a clean no-op (exits 0, writes nothing).
#   - Any other non-zero exit from render-setup-md.sh is a real error: propagates
#     non-zero so the caller knows the hook failed.
#   - render-setup-md.sh output is byte-stable, so re-running this hook when
#     nothing changed produces byte-identical content (no spurious workspace diff).
set -euo pipefail

: "${WINTER_WORKSPACE_DIR:?WINTER_WORKSPACE_DIR not set}"
: "${WINTER_EXT_DIR:?WINTER_EXT_DIR not set}"

render="$WINTER_EXT_DIR/workflow/render-setup-md.sh"
out="$WINTER_WORKSPACE_DIR/ai/project/setup-tmux.md"
tmp="$out.tmp.$$"

# Run the renderer, capturing its output and exit code without triggering set -e.
rendered=$("$render" "$WINTER_WORKSPACE_DIR" 2>/tmp/reconcile-workspace-render-err.$$) && rc=0 || rc=$?
render_stderr=$(cat /tmp/reconcile-workspace-render-err.$$ 2>/dev/null || true)
rm -f /tmp/reconcile-workspace-render-err.$$

if [[ $rc -eq 2 ]]; then
  # No committed setup-tmux.sh — this is expected on a fresh workspace.
  echo "reconcile-workspace: no setup-tmux.sh found; skipping setup-tmux.md generation" >&2
  exit 0
fi

if [[ $rc -ne 0 ]]; then
  echo "reconcile-workspace: render-setup-md.sh exited $rc — aborting setup-tmux.md write" >&2
  [[ -n "$render_stderr" ]] && printf '%s\n' "$render_stderr" >&2
  exit $rc
fi

# Write atomically via temp file: only replace the final path on success.
mkdir -p "$(dirname "$out")"
printf '%s\n' "$rendered" > "$tmp"
mv "$tmp" "$out"
echo "reconcile-workspace: wrote $out" >&2
