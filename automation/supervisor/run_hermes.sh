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

# Read the prompt content from the file (as the original script did)
prompt_content=$(cat "$prompt_file")

# Generate timestamp for the handoff
timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Build the enhanced prompt for the agent
cat > /tmp/enhanced_prompt.txt <<'ENHANCED_PROMPT'
You are an AI agent tasked with completing the slice of work: '$slice_id'. Your task is to make Hermes supervisor handoffs deterministic by modifying the script at automation/supervisor/run_hermes.sh to use hermes -z for non-interactive mode and to write the handoff JSON atomically.

After making the changes, you must run the required validations: 'make automation-check' and 'git diff --check'.

Then, you must output a valid JSON object that matches the following structure exactly (fill in the fields based on your work):

{
  "slice_id": "$slice_id",
  "status": "done" or "failed",
  "summary": "A summary of what was done",
  "files_touched": ["list of files changed"],
  "validations_passed": ["list of validations that passed"],
  "validations_failed": ["list of validations that failed"],
  "proof_level": "domain-tested",
  "missing_proof_levels": ["list of missing proof levels"],
  "contract_status_changes": [],
  "residual_risks": ["No known residual risk." or list of risks],
  "recommended_next_slice": "auth-add-offline-diagnostic-command",
  "recommended_next_reason": "Adapter fixed, can now run slice 2",
  "repo_clean_status": "clean" or "dirty",
  "git_mirror_status": "not-checked",
  "dirty_paths_outside_scope": ["list of paths"],
  "timestamp": "$timestamp"
}

If the validations pass, set status to "done". If they fail, set status to "failed" and adjust the fields accordingly.

Do not output any text other than the JSON.
ENHANCED_PROMPT

# Now prepend the original prompt content? Actually, the original prompt is the slice description.
# We'll combine them: our instructions + the original prompt.
# But note: the original prompt might be just the slice description. We'll put our instructions first, then a separator, then the original prompt.
final_prompt=$(cat /tmp/enhanced_prompt.txt)
final_prompt="${final_prompt}

--- Original prompt from supervisor ---
${prompt_content}"

# Clean up the temporary file
rm -f /tmp/enhanced_prompt.txt

# Run Hermes in one-shot mode with the prompt
hermes_output=$(hermes -z "$final_prompt" --toolsets coding --ignore-user-config --ignore-rules 2>&1)

# Validate and write JSON to handoff file
if echo "$hermes_output" | python3 -m.json.tool >/dev/null 2>&1; then
  echo "$hermes_output" > "$handoff_file"
  exit 0
else
  # Write failure handoff
  cat > "$handoff_file" <<'END_FAILURE'
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
    "timestamp": "$timestamp"
  }
  END_FAILURE
  exit 1
fi
