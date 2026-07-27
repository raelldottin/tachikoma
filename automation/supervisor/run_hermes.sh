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

cd "$repo_root"

# Run Hermes with the prompt, capturing output
# Hermes outputs JSON to stdout when -Q is used with -q for non-interactive mode
hermes_output=$(hermes chat -q "$(cat "$prompt_file")" -Q --ignore-user-config --ignore-rules --toolsets coding 2>&1)

# Extract JSON from output - Hermes with -Q outputs clean JSON as final response
# Find the JSON object in the output (starts with { or [)
json_start=$(echo "$hermes_output" | grep -n '^\{' | head -1 | cut -d: -f1)
if [[ -n "$json_start" ]]; then
    json_output=$(echo "$hermes_output" | tail -n +$json_start)
else
    # Fallback: assume last line is JSON
    json_output=$(echo "$hermes_output" | tail -1)
fi

# Validate and write JSON to handoff file
if echo "$json_output" | python3 -m json.tool >/dev/null 2>&1; then
    echo "$json_output" > "$handoff_file"
else
    # If output isn't valid JSON, create a minimal failed handoff
    cat > "$handoff_file" <<EOF
{
  "slice_id": "$slice_id",
  "status": "failed",
  "summary": "Agent failed to produce valid JSON handoff",
  "files_touched": [],
  "validations_passed": [],
  "validations_failed": [],
  "proof_level": "doc-only",
  "missing_proof_levels": ["domain-tested", "build-tested", "running-app-smoke", "flow-verified", "screenshot-verified", "device-verified", "testflight-verified"],
  "contract_status_changes": [],
  "residual_risks": ["Agent did not produce valid JSON handoff"],
  "recommended_next_slice": "",
  "recommended_next_reason": "",
  "repo_clean_status": "unknown",
  "git_mirror_status": "not-checked",
  "dirty_paths_outside_scope": [],
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF
    exit 1
fi