# Tachikoma Automation Workflow

This document describes the supervised automation workflow for the Tachikoma repository, using the reusable `repo-automation` framework.

## Overview

Tachikoma is a headless Pixel Starships maintenance agent. The automation framework provides a supervisor that runs queued slices sequentially, each slice being a validated unit of work with explicit allowed paths, validation commands, and handoff artifacts.

## Directory Structure

```
tachikoma/
├── AGENTS.md                     # Agent instructions (consumer-owned)
├── Makefile                      # Validation targets (consumer-owned)
├── automation/
│   ├── README.md                 # Framework documentation (synced)
│   ├── benchmark/                # Effectiveness benchmark (synced)
│   ├── context/                  # Context builder (synced)
│   ├── examples/                 # Example slices/handoffs (synced, templates)
│   ├── handoffs/                 # Handoff history (consumer-owned, .gitkeep)
│   ├── prompts/                  # Prompt templates (synced, templates)
│   ├── queue/
│   │   └── slices.json           # Work queue (consumer-owned)
│   ├── schemas/                  # JSON schemas (synced)
│   ├── supervisor/               # Supervisor implementation (synced)
│   └── tests/                    # Harness tests (synced)
├── docs/
│   └── workflows/
│       └── tachikoma-automation.md   # This file
├── Tools/
│   └── repo-automation-sync.sh   # Sync utility (synced)
├── run.py                        # Tachikoma entry point
└── sdk/                          # Tachikoma game SDK
```

## Consumer-Owned Files (Not Synced)

These files are created and owned by Tachikoma. They survive resyncs:

- `automation/queue/slices.json` — The work queue
- `automation/handoffs/` — Handoff artifacts (directory with `.gitkeep`)
- `AGENTS.md` — Agent instructions
- `Makefile` — Validation targets
- `.github/workflows/automation-check.yml` — CI workflow
- `docs/workflows/tachikoma-automation.md` — This documentation

## Framework Files (Synced from repo-automation)

These files are mirrored from `raelldottin/repo-automation` via `Tools/repo-automation-sync.sh`:

- `automation/README.md`
- `automation/benchmark/`
- `automation/context/`
- `automation/examples/` (templates)
- `automation/prompts/` (templates)
- `automation/reusable-manifest.json`
- `automation/schemas/`
- `automation/supervisor/`
- `automation/tests/test_harness.py`
- `automation/tests/test_benchmark_effectiveness.py`
- `automation/tests/fixtures/cmatrix.eval.json`
- `docs/workflows/repo-automation.md`
- `docs/workflows/effectiveness-benchmark.md`
- `Tools/repo-automation-sync.sh`
- `THIRD-PARTY-NOTICES.md`

## Resyncing the Framework

From the Tachikoma root:

```bash
./Tools/repo-automation-sync.sh \
  --sync \
  --source /path/to/repo-automation \
  --target .
```

## Prompt Customization

The base prompt at `automation/prompts/base.md` is a template. Tachikoma has customized it with:

- Tachikoma identity ("headless Pixel Starships maintenance agent")
- Safety rules forbidding real credentials, live network calls in tests, premium currency spending
- Preservation of existing gameplay strategy
- README.template → README.md workflow
- Supervisor ownership of queue and handoffs

Consumer prompt overrides survive normal resyncs. Use `--force-templates` to re-baseline from upstream.

## Validation Targets

The `Makefile` provides:

| Target | Runtime | Purpose |
|--------|---------|---------|
| `automation-check` | Python 3.11 | Run framework harness tests |
| `automation-dry-run` | Python 3.11 | Supervisor dry run (selects next slice) |
| `syntax-check` | Python 3.9 | Compile Tachikoma source |
| `test` | Python 3.9 | Run Tachikoma unit tests |
| `git-check` | — | Check for whitespace issues |

## CI Workflow

`.github/workflows/automation-check.yml` runs on PRs and pushes to main:

1. Python 3.11: `make automation-check`
2. Python 3.9: `make syntax-check` + `make git-check`

**No account secrets are used.** The scheduled account workflow (`.github/workflows/hourly-run.yml`) remains separate.

## Initial Queue

Two security/auth slices:

1. `security-remove-hardcoded-auth-material` — Remove embedded credentials, add redaction
2. `auth-add-offline-diagnostic-command` — Add `python -m tachikoma doctor --auth` with mocked responses

Policy: `consecutive_autonomous_limit: 2` — supervisor stops after two slices for human review.

## Running the Supervisor Dry Run

```bash
make automation-dry-run
```

Expected output:
- Selected slice: `security-remove-hardcoded-auth-material`
- Handoff path under `automation/handoffs/`
- No queue modification
- No child agent launch
- No network requests
- No GitHub secrets required

## Safety Rules

- Never use real credentials, refresh tokens, device identifiers, or account emails in source, fixtures, handoffs, logs, or tests
- Never contact live Pixel Starships endpoints during automated tests
- Never execute `run.py` with a real authentication string during validation
- Preserve existing training, research, room-upgrade, and resource-spending policy unless the current slice explicitly changes that policy
- Do not spend premium currency or add marketplace purchasing behavior unless the slice explicitly authorizes it and defines a spending limit
- README.md is generated from README.template; documentation changes should update README.template first
- The supervisor owns `automation/queue/slices.json` and `automation/handoffs/`
- Record worthwhile adjacent work in the handoff rather than implementing it