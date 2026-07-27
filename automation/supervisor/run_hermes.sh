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

# Use Hermes non-interactive chat mode with prompt from file
# -q reads the query from the argument, -Q suppresses UI elements for programmatic use
exec hermes chat -q "$(cat "$prompt_file")" -Q --ignore-user-config --ignore-rules --toolsets coding