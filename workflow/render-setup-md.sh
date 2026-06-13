#!/usr/bin/env bash
# Render the agent-facing setup-tmux.md from the project's committed
# setup-tmux.sh.
#
# This is a pure renderer: it sources the committed config for SESSION_PREFIX
# and WINTER_TMUX_SERVICE_NAMES, resolves each declared service to its
# <window>.<pane> target, and prints the canonical setup-tmux.md to stdout.
# It writes nothing. Writing is the caller's job:
#   - the workflow-setup walkthrough (ai/workflow-setup.md step 9) redirects this
#     into workspace:/ai/project/setup-tmux.md whenever setup-tmux.sh changes;
#   - doctor.sh renders and diffs to warn when the committed file drifts.
# (setup-tmux.md is a workspace-level artifact, so regeneration is NOT wired into
# the per-env on_env_init hook; a workspace-level hook is tracked in winter#47.)
#
# The gitignored setup-tmux.local.sh overlay is intentionally NOT sourced — the
# generated doc reflects committed config only (a machine-specific overlay is
# not committed context).
#
# Output is deterministic and byte-stable: same setup-tmux.sh in, same bytes out.
#
# Usage: render-setup-md.sh [workspace-dir]
#   workspace-dir defaults to $WINTER_WORKSPACE_DIR, then $(pwd).
# Exit status:
#   0  rendered to stdout
#   2  no committed setup-tmux.sh found
#   3  config failed to source, or SESSION_PREFIX is undefined
set -uo pipefail

WORKSPACE_DIR="${1:-${WINTER_WORKSPACE_DIR:-$(pwd)}}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HELPER="$SCRIPT_DIR/winter-service-tmux.sh"

# Resolve the committed config (no local overlay — the generated doc reflects
# committed config only).
project_dir="$WORKSPACE_DIR/ai/project"
config="$project_dir/setup-tmux.sh"

if [[ ! -f "$config" ]]; then
  echo "render-setup-md.sh: no setup-tmux.sh found at $project_dir" >&2
  exit 2
fi

# Extract the two values we render from, in a subshell so the user-authored
# config's vars/functions don't leak here. -u is disabled inside because the
# config isn't required to be -u-safe. Lines are tab-tagged so an entry can't be
# confused with the prefix; SESSION_PREFIX is required, the service array may be
# empty.
data=$(
  set +u
  # shellcheck source=winter-service-tmux.sh
  source "$HELPER" 2>/dev/null || exit 3
  # shellcheck source=/dev/null
  source "$config" 2>/dev/null || exit 3
  [[ -n "${SESSION_PREFIX:-}" ]] || exit 3
  printf 'PREFIX\t%s\n' "$SESSION_PREFIX"
  if [[ -n "${WINTER_TMUX_SERVICE_NAMES+x}" ]]; then
    for entry in "${WINTER_TMUX_SERVICE_NAMES[@]}"; do
      printf 'SVC\t%s\n' "$entry"
    done
  fi
) || {
  echo "render-setup-md.sh: $config failed to source or does not define SESSION_PREFIX" >&2
  exit 3
}

prefix=""
names=()
while IFS=$'\t' read -r kind val; do
  case "$kind" in
    PREFIX) prefix="$val" ;;
    SVC) names+=("$val") ;;
  esac
done <<< "$data"

# Render. Placeholders (<SESSION_PREFIX>, <worktree>, <session>, <window>,
# <pane>, <service>) stay literal — a reading agent fills them per-worktree.
# Only the e.g. session name and the bulleted service list use real values.
printf '# Service panes\n'
printf '\n'
printf 'Tmux session: `<SESSION_PREFIX>-<worktree>` (e.g. `%s-alpha`).\n' "$prefix"
printf '\n'
printf "Capture a service's output:\n"
printf '\n'
printf '    tmux capture-pane -t <session>:<window>.<pane> -p | tail -20\n'
printf '\n'
printf 'Restart one service in place (reap its pane, re-run its command):\n'
printf '\n'
printf '    ./restart <service>\n'
printf '\n'
printf 'Declared services:\n'
printf '\n'

# A bare entry "<name>" resolves to window 0, pane = its array index. A prefixed
# entry "<window>.<pane>:<name>" carries an explicit target. The index advances
# for every entry so bare entries keep their array position even when mixed.
# (Guard the loop so an empty array is safe under `set -u` on bash 3.2.)
if [[ ${#names[@]} -gt 0 ]]; then
  i=0
  for entry in "${names[@]}"; do
    if [[ "$entry" == *:* ]]; then
      target="${entry%%:*}"
      name="${entry#*:}"
    else
      target="0.$i"
      name="$entry"
    fi
    printf -- '- `%s` → `%s`\n' "$name" "$target"
    i=$((i + 1))
  done
fi
