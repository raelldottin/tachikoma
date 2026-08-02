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

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Add the script's parent directory to Python path so we can import automation
export PYTHONPATH="${SCRIPT_DIR}/..${PYTHONPATH:+:${PYTHONPATH}}"

export REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE="$context_file"
export REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export REPO_AUTOMATION_SUPERVISOR_SLICE_ID="$slice_id"
export OWLORY_SUPERVISOR_CONTEXT_FILE="$context_file"

# Set and export TIMESTAMP to avoid unbound variable errors
export TIMESTAMP="${TIMESTAMP:=$(date -u +'%Y-%m-%dT%H:%M:%SZ')}"

cd "$repo_root"

# Read prompt content from file
prompt_content=$(cat "$prompt_file")

# Default environment variables to avoid unbound variable errors with set -u
REPO_AUTOMATION_AGENT_RUNNER=${REPO_AUTOMATION_AGENT_RUNNER:-}
CLAUDE_CODE=${CLAUDE_CODE:-0}
OWLORY_SUPERVISOR_CONTEXT_FILE=${OWLORY_SUPERVISOR_CONTEXT_FILE:-}

# Choose agent and run
if [[ "$REPO_AUTOMATION_AGENT_RUNNER" == "codex" ]]; then
  # CODEX path (as expected by tests)
  agent_output=$(echo "$prompt_content" | codex \
    --ask-for-approval never \
    exec \
    --sandbox workspace-write \
    - \
    2>&1)
elif [[ "$CLAUDE_CODE" == "1" ]]; then
  # CLAUDE CODE path (unchanged from original)
  agent_output=$(claude \
    --print \
    --input-format text \
    --no-session-persistence \
    --permission-mode bypassPermissions \
    --add-dir "$repo_root" \
    --dangerously-skip-permissions \
    <<< "$prompt_content" 2>&1)
else
  # HERMES path: use chat -q -Q with HOMEBREW_NO_AUTO_UPDATE=1 for fast JSON output
  export HOMEBREW_NO_AUTO_UPDATE=1
  agent_output=$(hermes chat -q "$prompt_content" -Q --ignore-user-config --ignore-rules --toolsets coding 2>&1)
fi

# Export agent output for Python script
export AGENT_OUTPUT="$agent_output"
python_output_file=$(mktemp)
trap "rm -f $python_output_file" EXIT
python_exit_code=0
python3 << 'PYEOF' > "$python_output_file" || python_exit_code=$?
import sys
import json
import os
import re

agent_output = os.environ.get('AGENT_OUTPUT', '')
output = agent_output
lines = output.split('\n')

# Strategy 1: Find the last line that starts with { and is valid JSON
for i in range(len(lines) - 1, -1, -1):
    line = lines[i].strip()
    if line.startswith('{'):
        try:
            json.loads(line)
            print(line)
            sys.exit(0)
        except json.JSONDecodeError:
            pass

# Strategy 2: Find last valid multi-line JSON object
buffer = ''
for i in range(len(lines) - 1, -1, -1):
    buffer = lines[i] + '\n' + buffer
    try:
        json.loads(buffer)
        print(buffer.strip())
        sys.exit(0)
    except json.JSONDecodeError:
        pass

# Strategy 3: Regex find all JSON-like patterns, validate backwards
matches = list(re.finditer(r'\{.*?\}', output, re.DOTALL))
for match in reversed(matches):
    try:
        json.loads(match.group())
        print(match.group())
        sys.exit(0)
    except json.JSONDecodeError:
        pass

# Last resort: exit with error
sys.exit(1)
PYEOF
json_output=$(cat "$python_output_file")

# Check if JSON extraction succeeded
if [[ $python_exit_code -ne 0 ]] || [[ -z "$json_output" ]]; then
  # Write failure handoff atomically
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
handoff_tmp = os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'] + '.tmp'
with open(handoff_tmp, 'w') as f:
  json.dump(data, f, indent=2)
os.rename(handoff_tmp, os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'])
"
  exit 0
fi

# Validate JSON syntax
if ! echo "$json_output" | python3 -m json.tool >/dev/null 2>&1; then
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
handoff_tmp = os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'] + '.tmp'
with open(handoff_tmp, 'w') as f:
  json.dump(data, f, indent=2)
os.rename(handoff_tmp, os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'])
"
  exit 0
fi

# Validate slice_id matches
if ! echo "$json_output" | python3 -c "
import json
import sys
import os
data = json.load(sys.stdin)
expected = os.environ.get('REPO_AUTOMATION_SUPERVISOR_SLICE_ID', '')
if data.get('slice_id') != expected:
    print('slice_id mismatch: expected %s, got %s' % (expected, data.get('slice_id')), file=sys.stderr)
    sys.exit(1)
" 2>&1; then
  python3 -c "
import json
import sys
import os

data = {
  'slice_id': os.environ.get('REPO_AUTOMATION_SUPERVISOR_SLICE_ID', 'unknown'),
  'status': 'failed',
  'summary': 'Agent handoff slice_id does not match expected slice',
  'files_touched': [],
  'validations_passed': [],
  'validations_failed': [],
  'proof_level': 'doc-only',
  'missing_proof_levels': ['domain-tested', 'build-tested', 'running-app-smoke', 'flow-verified', 'screenshot-verified', 'device-verified', 'testflight-verified'],
  'contract_status_changes': [],
  'residual_risks': ['Handoff slice_id mismatch'],
  'recommended_next_slice': '',
  'recommended_next_reason': '',
  'repo_clean_status': 'unknown',
  'git_mirror_status': 'not-checked',
  'dirty_paths_outside_scope': [],
  'timestamp': os.environ.get('TIMESTAMP', '')
}
handoff_tmp = os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'] + '.tmp'
with open(handoff_tmp, 'w') as f:
  json.dump(data, f, indent=2)
os.rename(handoff_tmp, os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'])
"
  exit 0
fi

# Write JSON to temporary file for policy validation
handoff_tmp="${handoff_file}.tmp"
echo "$json_output" > "$handoff_tmp"

# Validate against handoff schema using policy module
if ! python3 -c "
import json
import sys
import os
from pathlib import Path
from automation.supervisor import policy

try:
    handoff = policy.load_handoff(
        Path('$handoff_tmp'),
        Path('.') / 'automation/schemas/handoff.schema.json'
    )
    
    # Additional check: verify slice_id matches expected
    expected_slice_id = os.environ.get('REPO_AUTOMATION_SUPERVISOR_SLICE_ID', '')
    if handoff['slice_id'] != expected_slice_id:
        print('slice_id mismatch: expected %s, got %s' % (expected_slice_id, handoff['slice_id']), file=sys.stderr)
        sys.exit(1)
    
    sys.exit(0)
except Exception as e:
    print('Schema validation failed: %s' % e, file=sys.stderr)
    sys.exit(1)
" 2>&1; then
  # Validation failed - create failure handoff atomically
  python3 -c "
import json
import sys
import os

data = {
  'slice_id': os.environ.get('REPO_AUTOMATION_SUPERVISOR_SLICE_ID', 'unknown'),
  'status': 'failed',
  'summary': 'Handoff JSON failed schema validation',
  'files_touched': [],
  'validations_passed': [],
  'validations_failed': ['schema-validation'],
  'proof_level': 'doc-only',
  'missing_proof_levels': ['domain-tested', 'build-tested', 'running-app-smoke', 'flow-verified', 'screenshot-verified', 'device-verified', 'testflight-verified'],
  'contract_status_changes': [],
  'residual_risks': ['Handoff JSON does not conform to required schema'],
  'recommended_next_slice': '',
  'recommended_next_reason': '',
  'repo_clean_status': 'unknown',
  'git_mirror_status': 'not-checked',
  'dirty_paths_outside_scope': [],
  'timestamp': os.environ.get('TIMESTAMP', '')
}
handoff_tmp = os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'] + '.tmp'
with open(handoff_tmp, 'w') as f:
  json.dump(data, f, indent=2)
os.rename(handoff_tmp, os.environ['REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE'])
"
  # Clean up temp file
  rm -f "$handoff_tmp"
  exit 0
fi

# Validation passed - move temp file to final location atomically
mv "$handoff_tmp" "$handoff_file"
exit 0