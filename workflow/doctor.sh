#!/usr/bin/env bash
# Doctor probe for winter-service-tmux.
#
# Emits NDJSON to stdout per the contract documented in
# workspace:/ai/winter-cli/setup.md#doctor-probes. One object per line:
#   {"name": "...", "status": "pass|warn|fail", "message"?: "...", "remediation"?: "..."}
#
# Three checks:
#   1. tmux binary on PATH
#   2. SESSION_PREFIX declared in workspace:/ai/project/workflow.sh
#   3. session-name collision with foreign tmux sessions sharing the prefix
#
# Each probe is implemented as an explicit branch that emits its own NDJSON;
# the script exits 0 at the end so per-probe statuses surface individually.
# A non-zero exit would be collapsed by `winter doctor` into a single
# synthetic `fail` and mask the per-check details.
set -uo pipefail

WORKSPACE_DIR="${WINTER_WORKSPACE_DIR:-$(pwd)}"
WORKFLOW_SH="$WORKSPACE_DIR/ai/project/workflow.sh"

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/\\r}"
  printf '%s' "$s"
}

emit() {
  local name status message remediation
  name=$(json_escape "$1")
  status="$2"
  message=$(json_escape "${3:-}")
  remediation=$(json_escape "${4:-}")
  if [[ -n "${4:-}" ]]; then
    printf '{"name":"%s","status":"%s","message":"%s","remediation":"%s"}\n' \
      "$name" "$status" "$message" "$remediation"
  elif [[ -n "${3:-}" ]]; then
    printf '{"name":"%s","status":"%s","message":"%s"}\n' \
      "$name" "$status" "$message"
  else
    printf '{"name":"%s","status":"%s"}\n' "$name" "$status"
  fi
}

# ---- Probe 1: tmux binary -----------------------------------------------------

tmux_ok=false
if command -v tmux >/dev/null 2>&1 && tmux_version=$(tmux -V 2>/dev/null); then
  tmux_ok=true
  emit "tmux binary" pass "$tmux_version"
else
  emit "tmux binary" fail \
    "tmux not found on PATH" \
    "Install tmux (e.g. \`dnf install tmux\`, \`brew install tmux\`)."
fi

# ---- Probe 2: SESSION_PREFIX declared -----------------------------------------

session_prefix=""
session_prefix_ok=false

if [[ ! -f "$WORKFLOW_SH" ]]; then
  emit "SESSION_PREFIX declared" fail \
    "workflow.sh not found at ai/project/workflow.sh" \
    "Run the workflow-setup walkthrough at winter-service-tmux:/ai/workflow-setup.md."
else
  # Source in a subshell so workflow.sh's functions/vars don't leak into our
  # environment. Disable -u inside the subshell because workflow.sh isn't
  # required to be -u-safe (it's user-authored). Status and value are joined
  # by US (\x1f) so an empty SESSION_PREFIX survives $()'s trailing-newline
  # stripping.
  source_result=$(
    set +u
    if source "$WORKFLOW_SH" 2>/dev/null; then
      printf 'ok\x1f%s' "${SESSION_PREFIX:-}"
    else
      printf 'fail\x1f'
    fi
  )
  source_status="${source_result%%$'\x1f'*}"
  source_value="${source_result#*$'\x1f'}"

  case "$source_status" in
    ok)
      if [[ -n "$source_value" ]]; then
        session_prefix="$source_value"
        session_prefix_ok=true
        emit "SESSION_PREFIX declared" pass "SESSION_PREFIX=$session_prefix"
      else
        emit "SESSION_PREFIX declared" fail \
          "workflow.sh does not define SESSION_PREFIX" \
          "Re-run the workflow-setup walkthrough at winter-service-tmux:/ai/workflow-setup.md."
      fi
      ;;
    *)
      emit "SESSION_PREFIX declared" fail \
        "workflow.sh failed to source (syntax error or runtime failure)" \
        "Fix workspace:/ai/project/workflow.sh, then re-run \`winter doctor\`."
      ;;
  esac
fi

# ---- Probe 3: session-name collision ------------------------------------------

if [[ "$tmux_ok" != true ]]; then
  emit "session-name collision" warn "skipped: tmux not installed"
elif [[ "$session_prefix_ok" != true ]]; then
  emit "session-name collision" warn "skipped: SESSION_PREFIX undetermined"
else
  # `tmux ls` exits non-zero when no tmux server is running; treat that as
  # "no collision possible" rather than an error.
  if ! sessions=$(tmux ls -F '#{session_name}' 2>/dev/null); then
    emit "session-name collision" pass "no tmux server running"
  else
    conflicting=()
    while IFS= read -r session; do
      [[ -z "$session" ]] && continue
      [[ "$session" == "$session_prefix"-* ]] || continue
      env_name="${session#"$session_prefix"-}"
      # A session is "ours" iff `<workspace>/<env_name>/` is a feature env —
      # i.e. has a `.winter.env` file (seeded by `winter ws init`). Plain
      # directory existence is too weak: the workspace root also contains
      # source checkouts, helper dirs (`tools/`, `projects/`, `docs/`), and
      # standalone extension clones whose names could otherwise mask a real
      # collision.
      if [[ -f "$WORKSPACE_DIR/$env_name/.winter.env" ]]; then
        continue
      fi
      conflicting+=("$session")
    done <<< "$sessions"

    if [[ ${#conflicting[@]} -eq 0 ]]; then
      emit "session-name collision" pass "no foreign tmux sessions match \`${session_prefix}-*\`"
    else
      emit "session-name collision" warn \
        "foreign tmux sessions match \`${session_prefix}-*\`: ${conflicting[*]}" \
        "Rename SESSION_PREFIX in workflow.sh or stop the foreign sessions."
    fi
  fi
fi

exit 0
