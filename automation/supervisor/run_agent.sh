#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: automation/supervisor/run_agent.sh \
  --repo-root PATH \
  --prompt-file PATH \
  --context-file PATH \
  --handoff-file PATH \
  --slice-id ID

Launch a fresh agent run for one supervisor-selected slice.

Runner selection:
  REPO_AUTOMATION_AGENT_RUNNER=auto|codex|claude (default: auto)
  REPO_AUTOMATION_CODEX_BIN overrides the Codex executable.
  REPO_AUTOMATION_CLAUDE_BIN overrides the Claude Code executable.
  REPO_AUTOMATION_CLAUDE_PERMISSION_MODE overrides the Claude permission mode.

Legacy OWLORY_CODEX_BIN remains supported for Codex executable overrides.
USAGE
}

process_tree_contains() {
  local needle="$1"
  local pid="${PPID:-}"
  local command=""
  local args=""

  while [[ -n "$pid" && "$pid" != "0" ]]; do
    command="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
    args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
    if [[ "$command" == *"$needle"* || "$args" == *"$needle"* ]]; then
      return 0
    fi
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  done

  return 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

running_under_claude_code() {
  [[ -n "${CLAUDECODE:-}" || -n "${CLAUDE_CODE:-}" || -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]] ||
    process_tree_contains "claude"
}

select_agent_runner() {
  local requested_runner="$1"
  local codex_bin="$2"
  local claude_bin="$3"

  case "$requested_runner" in
    auto)
      if running_under_claude_code && command_exists "$claude_bin"; then
        printf 'claude\n'
      elif command_exists "$codex_bin"; then
        printf 'codex\n'
      elif command_exists "$claude_bin"; then
        printf 'claude\n'
      else
        echo "No supported agent CLI found. Install Codex or Claude Code, or set REPO_AUTOMATION_AGENT_RUNNER with a matching binary override." >&2
        return 69
      fi
      ;;
    codex|claude)
      printf '%s\n' "$requested_runner"
      ;;
    *)
      echo "Unsupported REPO_AUTOMATION_AGENT_RUNNER: $requested_runner" >&2
      echo "Expected one of: auto, codex, claude." >&2
      return 64
      ;;
  esac
}

repo_root=""
prompt_file=""
context_file=""
handoff_file=""
slice_id=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      repo_root="${2:-}"
      shift 2
      ;;
    --prompt-file)
      prompt_file="${2:-}"
      shift 2
      ;;
    --context-file)
      context_file="${2:-}"
      shift 2
      ;;
    --handoff-file)
      handoff_file="${2:-}"
      shift 2
      ;;
    --slice-id)
      slice_id="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 64
      ;;
  esac
done

if [[ -z "$repo_root" || -z "$prompt_file" || -z "$context_file" || -z "$handoff_file" || -z "$slice_id" ]]; then
  echo "Missing required supervisor agent launch argument." >&2
  usage
  exit 64
fi

if [[ ! -d "$repo_root/.git" ]]; then
  echo "Repo root is not a Git checkout: $repo_root" >&2
  exit 66
fi

if [[ ! -f "$prompt_file" ]]; then
  echo "Prompt file does not exist: $prompt_file" >&2
  exit 66
fi

if [[ ! -f "$context_file" ]]; then
  echo "Context file does not exist: $context_file" >&2
  exit 66
fi

if [[ -e "$handoff_file" ]]; then
  echo "Refusing to overwrite existing handoff file: $handoff_file" >&2
  exit 73
fi

codex_bin="${REPO_AUTOMATION_CODEX_BIN:-${OWLORY_CODEX_BIN:-codex}}"
claude_bin="${REPO_AUTOMATION_CLAUDE_BIN:-claude}"
agent_runner="$(select_agent_runner "${REPO_AUTOMATION_AGENT_RUNNER:-auto}" "$codex_bin" "$claude_bin")"

export REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE="$context_file"
export REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export REPO_AUTOMATION_SUPERVISOR_SLICE_ID="$slice_id"
export OWLORY_SUPERVISOR_CONTEXT_FILE="$context_file"
export OWLORY_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export OWLORY_SUPERVISOR_SLICE_ID="$slice_id"

cd "$repo_root"

case "$agent_runner" in
  codex)
    if ! command_exists "$codex_bin"; then
      echo "Codex CLI not found. Set REPO_AUTOMATION_CODEX_BIN, OWLORY_CODEX_BIN, or install codex." >&2
      exit 69
    fi
    exec "$codex_bin" --ask-for-approval never exec \
      -C "$repo_root" \
      --sandbox workspace-write \
      - < "$prompt_file"
    ;;
  claude)
    if ! command_exists "$claude_bin"; then
      echo "Claude Code CLI not found. Set REPO_AUTOMATION_CLAUDE_BIN or install claude." >&2
      exit 69
    fi
    exec "$claude_bin" --print \
      --input-format text \
      --no-session-persistence \
      --permission-mode "${REPO_AUTOMATION_CLAUDE_PERMISSION_MODE:-bypassPermissions}" \
      --add-dir "$repo_root" \
      < "$prompt_file"
    ;;
esac
