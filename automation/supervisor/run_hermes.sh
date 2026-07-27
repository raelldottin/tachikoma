#!/usr/bin/env bash
set -euo pipefail

repo_root=""
prompt_file=""
context_file=""
handoff_file=""
slice_id=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) repo_root="$2"; shift 2 ;;
    --prompt-file) prompt_file="$2"; shift 2 ;;
    --context-file) context_file="$2"; shift 2 ;;
    --handoff-file) handoff_file="$2"; shift 2 ;;
    --slice-id) slice_id="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 64 ;;
  esac
done

export REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE="$context_file"
export REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export REPO_AUTOMATION_SUPERVISOR_SLICE_ID="$slice_id"
export OWLORY_SUPERVISOR_CONTEXT_FILE="$context_file"

cd "$repo_root"

# Read prompt content from file
prompt_content=$(cat "$prompt_file")

# Default environment variables to avoid unbound variable errors with set -u
REPO_AUTOMATION_AGENT_RUNNER=${REPO_AUTOMATION_AGENT_RUNNER:-}
CLAUDE_CODE=${CLAUDE_CODE:-0}
OWLORY_SUPERVISOR_CONTEXT_FILE=${OWLORY_SUPERVISOR_CONTEXT_FILE:-}

export REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE="$context_file"
export REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export REPO_AUTOMATION_SUPERVISOR_SLICE_ID="$slice_id"
export OWLORY_SUPERVISOR_CONTEXT_FILE="$context_file"

cd "$repo_root"

# Read prompt content from file
prompt_content=$(cat "$prompt_file")

# Choose agent based on environment variables
if [[ "$REPO_AUTOMATION_AGENT_RUNNER" == "codex" ]]; then
  # CODEX path (as expected by tests)
  echo "$prompt_content" | /Users/raelldottin/.local/bin/codex \
    --ask-for-approval never exec --sandbox workspace-write - \
    2>&1
elif [[ "$CLAUDE_CODE" == "1" ]]; then
  # CLAUDE CODE path (unchanged from original)
  /Users/raelldottin/.local/bin/claude \
    --print \
    --input-format text \
    --no-session-persistence \
    --permission-mode bypassPermissions \
    --add-dir "$repo_root" \
    --dangerously-skip-permissions \
    <<< "$prompt_content" \
    2>&1
else
  # HERMES path: use chat -q -Q with HOMEBREW_NO_AUTO_UPDATE=1 for fast JSON output
  export HOMEBREW_NO_AUTO_UPDATE=1
  hermes_output=$(hermes chat -q "$prompt_content" -Q --ignore-user-config --ignore-rules --toolsets coding 2>&1)

  # Extract JSON from output - find first line that starts with {
  json_line=$(echo "$hermes_output" | grep -n '^{' | head -1 | cut -d: -f1)
  if [[ -n "$json_line" ]]; then
    json_output=$(echo "$hermes_output" | tail -n +"$json_line")
  else
    # Fallback: last non-empty line
    json_output=$(echo "$hermes_output" | grep -v '^$' | tail -1)
  fi

  # Validate and write JSON to handoff file using python json.tool (no external deps)
  if echo "$json_output" | python3 -m json.tool >/dev/null 2>&1; then
    echo "$json_output" > "$handoff_file"
    exit 0
  else
    # Write failure handoff using python to avoid heredoc issues
    python3 -c "
import json
import sys
import os

data = {
  'slice_id': os.environ.get('REPO_AUTOMATION_SUPERVISOR_SLICE_ID', 'unknown'),
  'status': 'failed',
  'summary': 'Agent failed to produce valid JSON handoff',
  'files_touched': [],
  'validations_passed': [],
  'validations_failed': [],
  'proof_level': 'doc-only',
  'missing_proof_levels': ['domain-tested', 'build-tested', 'running-app-smoke', 'flow-verified', 'screenshot-verified', 'device-verified', 'testflight-verified'],
  'contract_status_changes': [],
  'residual_risks': ['Agent did not produce valid JSON handoff'],
  'recommended_next_slice': '',
  'recommended_next_reason': '',
  'repo_clean_status': 'unknown',
  'git_mirror_status': 'not-checked',
  'dirty_paths_outside_scope': [],
  'timestamp': os.environ.get('TIMESTAMP', '')
}
with open(os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'], 'w') as f:
    json.dump(data, f, indent=2)
"
    exit 1
  fi
fi