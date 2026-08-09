## 2026-08-06T05:43:47Z
You are teamwork_preview_worker_m2_1.
Your working directory is: /Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_worker_m2_1

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

MANDATORY ASSIGNMENT:
Read /Users/raelldottin/Documents/Personal/tachikoma/ORIGINAL_REQUEST.md and AGENTS.md before starting work.

Task: Milestone 2 - Define Pilot Slice Metadata & Build Independent Critic Loop Infrastructure (R3 & R5)
Files you are exclusively authorized to create/write:
- `automation/schemas/critic_review.schema.json`
- `automation/gauntlet/critic_prompt.md`
- `automation/gauntlet/slice_definition.json`

Instructions:
1. Create `automation/schemas/critic_review.schema.json`:
   Construct a JSON Schema (Draft-07 compliant) defining the strict JSON format required for Independent Critic reviews per ORIGINAL_REQUEST.md §R5:
   - `$schema`: "http://json-schema.org/draft-07/schema#"
   - `type`: "object"
   - Required properties: `verdict`, `largest_remaining_gap`, `severity`, `evidence`, `quality_bar_failures`, `required_next_action`
   - `verdict`: enum `["pass", "fail"]`
   - `severity`: enum `["none", "low", "medium", "high", "critical"]`
   - `largest_remaining_gap`: string (empty if verdict is pass)
   - `evidence`: array of strings
   - `quality_bar_failures`: array of integers (1 through 12)
   - `required_next_action`: string
   - `additionalProperties`: false

2. Create `automation/gauntlet/critic_prompt.md`:
   Define the standardized prompt template for spawning fresh Independent Critic runs per R5:
   - Explicitly mandate context isolation: Critic receives ONLY slice goal, constraints, AGENTS.md, quality-bar.md, relevant codebase files, before/after diff, changed files list, exact validation outputs, test/lint outputs, sanitized workflow logs, and evidence artifacts.
   - MUST NOT include builder reasoning, builder self-review, or unsupported builder claims.
   - Instruct Critic to evaluate against `quality-bar.md` and return strictly valid JSON matching `automation/schemas/critic_review.schema.json`.

3. Create `automation/gauntlet/slice_definition.json`:
   Define the pilot slice configuration per R3:
   - `slice_id`: "gauntlet-provisioning-dependency-and-secret-contract"
   - `title`: "Gauntlet Provisioning Dependency and Secret Contract Pilot"
   - `max_files_changed`: 10
   - `allowed_paths`:
     - ".github/workflows/provision-pss-secrets.yml"
     - "scripts/provision_account_secrets.py"
     - "tests/"
     - "automation/gauntlet/"
     - "automation/prompts/"
     - "automation/schemas/"
     - "automation/tests/"
     - "requirements.txt"
     - "Makefile"
     - "docs/workflows/"
     - "README.template"
     - "README.md"
   - `required_validations`: ["make automation-check", "make syntax-check", "make test", "make test-security", "make lint", "git diff --check"]

4. Validate created JSON files using `python3 -m json.tool` to ensure syntax validity.
5. Write a self-contained handoff report to `/Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_worker_m2_1/handoff.md`.

When finished, send a message to parent with your handoff summary and path.
