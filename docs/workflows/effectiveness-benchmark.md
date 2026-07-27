# Effectiveness Benchmark

The effectiveness benchmark measures how good the harness is at producing *working*
code — not how fast its functions run (that is the CodSpeed micro-benchmark). It uses
[ProgramBench](https://github.com/facebookresearch/ProgramBench): the harness rebuilds
real programs from scratch and is scored by ProgramBench's black-box test suites.

## What it measures

For each ProgramBench instance the harness attempts a rebuild; ProgramBench then runs
the program's hidden test suite. The report (`effectiveness-report.json`) aggregates:

- **resolve rate** — fraction of instances where every test passed (`pass_fraction >= 1.0`).
- **near-resolve rate** — fraction with `pass_fraction >= 0.95`.
- **mean pass fraction** — average test pass fraction across instances.
- **error counts** — ProgramBench `error_code`s (e.g. `copy_executable_failed`), per instance.

## Architecture

| Component | Role | Containers? |
|-----------|------|-------------|
| `automation/benchmark/adapter.py` | Drives the supervisor loop (`run_next` + `run_agent.sh`) to rebuild a program in a local workspace and archive it as `submission.tar.gz` | No |
| `automation/benchmark/evalrunner.py` | `ProgramBenchEvalRunner` shells out to `programbench eval` (authoritative test run) | **Yes** (amd64) |
| `automation/benchmark/scoring.py` | Reduces eval JSON to the effectiveness report | No |
| `automation/benchmark/run.py` | CLI + orchestrator (`run` / `eval` / `score` / `all`) | only `eval`/`all` |

The agent rebuilds on the local filesystem; ProgramBench's cleanroom Docker images are
used only by the `eval` step, so everything except the authoritative test run is
container-free and unit-tested with no Docker or LLM.

## Running it

### Score existing eval output (no Docker)

```shell
uv run python -m automation.benchmark score --run-dir path/to/run-dir
```

### Full run (rebuild → eval → score)

`eval` needs Docker and the amd64 cleanroom images, so run it on a native x86_64 Linux
host (or in CI). Point the agent at an OpenAI-compatible LLM — e.g. NVIDIA:

```shell
export REPO_AUTOMATION_AGENT_RUNNER=codex
export OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1
export OPENAI_API_KEY="$NVIDIA_API_KEY"

uv pip install programbench
uv run python -m automation.benchmark all --run-dir out --all          # full set
uv run python -m automation.benchmark all --run-dir out --instances abishekvashok__cmatrix.5c082c6
```

> On Apple Silicon the cleanroom images run only under slow amd64 emulation; prefer an
> x86_64 host for real runs.

## In production (CI)

`.github/workflows/effectiveness-benchmark.yml` runs the full set on `ubuntu-latest`
(native x86_64) on a nightly schedule and on manual dispatch. It requires the
repository secret **`NVIDIA_API_KEY`** and uploads `effectiveness-report.json` plus the
per-instance `*.eval.json` files as a build artifact. The workflow is repo-specific and
is not part of the reusable sync manifest.

## Reading the report

`effectiveness-report.json` holds the aggregate metrics and a per-instance breakdown.
The `score` / `all` commands also print a summary table:

```
Harness Effectiveness (ProgramBench)
  instances        : 1
  resolved         : 0 (0.0%)
  near-resolved    : 0 (0.0%)
  mean pass frac.  : 0.0%
  errors           : copy_executable_failed=1
```
