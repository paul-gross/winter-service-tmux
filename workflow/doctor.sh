#!/usr/bin/env bash
# Doctor probe for winter-service-tmux.
#
# Emits NDJSON to stdout per the contract documented in
# workspace:/ai/winter-cli/configuration/doctor.md#probe-output-contract. One object per line:
#   {"name": "...", "status": "pass|warn|fail", "message"?: "...", "remediation"?: "..."}
#
# Four checks:
#   1. tmux binary on PATH
#   2. config.toml manifest present and valid (required — this is now live config)
#   3. session-name collision with foreign tmux sessions sharing the prefix
#   4. layout_hook exists and is executable (when declared in the manifest)
#
# Each probe is implemented as an explicit branch that emits its own NDJSON;
# the script exits 0 at the end so per-probe statuses surface individually.
# A non-zero exit would be collapsed by `winter doctor` into a single
# synthetic `fail` and mask the per-check details.
set -uo pipefail

WORKSPACE_DIR="${WINTER_WORKSPACE_DIR:-$(pwd)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_SRC="$SCRIPT_DIR/../src"

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

# ---- Probe 2: config.toml manifest ----------------------------------------
#
# The manifest is now live config (not optional). A missing or invalid manifest
# is a real finding (fail). Validates via the service_manifest CLI (stdlib-only,
# python3 required). Populates session_prefix for probe 3.
#
# Config dir is resolved via WINTER_EXT_CONFIG_DIR (set by winter on every
# orchestrator dispatch) or falls back to
# <workspace-root>/.winter/config/winter-service-tmux/.

if [[ -n "${WINTER_EXT_CONFIG_DIR:-}" ]]; then
  CONFIG_DIR="$WINTER_EXT_CONFIG_DIR"
else
  CONFIG_DIR="$WORKSPACE_DIR/.winter/config/winter-service-tmux"
fi

MANIFEST_PATH="$CONFIG_DIR/config.toml"

manifest_ok=false
session_prefix=""

if [[ ! -f "$MANIFEST_PATH" ]]; then
  emit "config.toml manifest" fail \
    "no config.toml found at $CONFIG_DIR" \
    "Create $MANIFEST_PATH — see winter-service-tmux:/workflow/config.toml.example."
else
  # Locate a python interpreter.
  PY=""
  if command -v python3 >/dev/null 2>&1; then
    PY="python3"
  elif command -v python >/dev/null 2>&1; then
    PY="python"
  fi

  if [[ -z "$PY" ]]; then
    emit "config.toml manifest" warn "skipped: python3 not found"
  else
    # Run the validator; capture stdout and exit code.
    cli_out=$(PYTHONPATH="$EXT_SRC" "$PY" -m service_manifest.cli validate "$WORKSPACE_DIR" --json 2>/dev/null)
    cli_exit=$?

    if [[ $cli_exit -ne 0 ]] && ! printf '%s' "$cli_out" | grep -q '"ok"'; then
      # Non-zero exit with no parseable JSON — likely Python < 3.11 / import failure.
      emit "config.toml manifest" warn \
        "skipped: manifest validation unavailable (Python 3.11+ required)"
    else
      # Parse the JSON output with python to avoid whitespace/quoting fragility.
      parsed=$(printf '%s' "$cli_out" | "$PY" -c '
import json, sys
data = json.load(sys.stdin)
ok = data.get("ok", False)
violations = data.get("violations", [])
print("true" if ok else "false")
print(len(violations))
print("; ".join(violations))
')
      ok_val=$(printf '%s' "$parsed" | sed -n '1p')
      vcount=$(printf '%s' "$parsed" | sed -n '2p')
      violations_msg=$(printf '%s' "$parsed" | sed -n '3p')

      if [[ "$ok_val" == "true" ]]; then
        manifest_ok=true
        emit "config.toml manifest" pass "config.toml valid"
      else
        emit "config.toml manifest" fail \
          "${vcount} violation(s): ${violations_msg}" \
          "Fix $MANIFEST_PATH, then re-run \`winter doctor\`."
      fi
    fi

    # Extract session_prefix from the TOML for probe 3 (simple grep; the field
    # is a top-level scalar so the pattern is reliable).
    session_prefix=$(grep -E '^[[:space:]]*session_prefix[[:space:]]*=' "$MANIFEST_PATH" \
      | sed 's/.*=[[:space:]]*//' | tr -d '"'"'"' ')
  fi
fi

# ---- Probe 3: session-name collision ------------------------------------------

if [[ "$tmux_ok" != true ]]; then
  emit "session-name collision" warn "skipped: tmux not installed"
elif [[ "$manifest_ok" != true ]]; then
  emit "session-name collision" warn "skipped: manifest absent or invalid"
elif [[ -z "$session_prefix" ]]; then
  emit "session-name collision" warn "skipped: session_prefix not found in config.toml"
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
        "Rename session_prefix in config.toml or stop the foreign sessions."
    fi
  fi
fi

# ---- Probe 4: layout_hook exists and is executable ----------------------------
#
# Reads the committed config.toml only. The gitignored config.local.toml overlay
# may declare a different layout_hook; that override is not seen by this bash
# sub-check.
#
# layout_hook is a bare filename resolved relative to CONFIG_DIR (alongside
# config.toml), not relative to the workspace root.

if [[ -f "$MANIFEST_PATH" ]]; then
  layout_hook_val=$(grep -E '^[[:space:]]*layout_hook[[:space:]]*=' "$MANIFEST_PATH" \
    | sed 's/.*=[[:space:]]*//' | tr -d '"'"'"' ')

  if [[ -n "$layout_hook_val" ]]; then
    hook_path="$CONFIG_DIR/$layout_hook_val"
    if [[ -x "$hook_path" ]]; then
      emit "config.toml layout_hook" pass "layout_hook exists and is executable: $layout_hook_val"
    elif [[ -f "$hook_path" ]]; then
      emit "config.toml layout_hook" warn \
        "layout_hook file exists but is not executable: $layout_hook_val" \
        "Run: chmod +x $hook_path"
    else
      emit "config.toml layout_hook" warn \
        "layout_hook file not found: $layout_hook_val" \
        "Create the file at $hook_path or remove layout_hook from config.toml."
    fi
  fi
fi

exit 0
