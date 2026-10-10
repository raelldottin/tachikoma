# Real NVIDIA Validation Plan (draft — not yet authorized)

Status: PLANNING ONLY. Live testing requires explicit authorization from
dasdot. Zero-credit constraint applies until then; nothing in this plan
has been run against `https://integrate.api.nvidia.com/v1`.

## Objective

Establish whether the three production NVIDIA credentials provide three
independent pools of inference capacity (independent rate limits), and
verify the gateway behaves correctly against the real upstream — the two
properties the mock suite cannot prove (dasdot's rollout table rows
"Independent NVIDIA account capacity" and live endpoint behavior).

## Credentials and security controls

- 3× NVIDIA API keys (NVIDIA_KEY_1/2/3), provided by dasdot OUT of band
  (1Password vault or env injection on the host). Never through Discord,
  never committed, never echoed into logs.
- Gateway credential (GATEWAY_MASTER_KEY) stays a synthetic secret.
- Redaction gate from the mock suite applies verbatim: after the run,
  `grep` proxy logs for each key substring and any `Bearer` value —
  expected 0 hits; any hit fails the run.
- Keys are provisioned per-run via environment, matching the sanitized
  `config.yaml` (`os.environ/NVIDIA_KEY_1` etc.).

## Test matrix (limited live run)

Each test is ONE streaming request unless noted. Model: z-ai/glm-5.3,
api_base https://integrate.api.nvidia.com/v1, via gateway with the
streaming-integrity patch applied and validate_deployment.py green first.

| # | Test | Measures | Expected |
|---|---|---|---|
| L1 | Single non-streaming request | basic reachability, latency | 200, sane content |
| L2 | Single streaming request | SSE integrity live | 200, genuine finish_reason + [DONE] |
| L3 | Streaming request, capture provider request IDs | provider-side observability | IDs present in response headers/log, distinct per request |
| L4 | 3 sequential streaming requests, one per pinned key | per-key behavior parity | all 3 succeed; latency variance noted |
| L5 | Per-key throughput probes: N=5 requests per key, sequential, spaced | independent quota pools | if limits are per-account: no 429s at low N; note any 429/Retry-After headers per key |
| L6 | Controlled 429: fire requests per key until 429 or cap reached (cap: 20/key) | real rate-limit ceiling + recovery | 429 surfaces to client as explicit failure (not synthetic success); Retry-After respected; cooldown then recovery observed |
| L7 | Credential-failure live check: revoke/disable ONE key temporarily (dasdot action) | gateway failover live | 401 from upstream → deployment cooldown → traffic shifts to other 2 keys; no infinite retry; bounded error if all keys dead |
| L8 | Retry behavior under live 5xx | bounded retries | ≤ num_retries (2) attempts, explicit failure after |

Recorded per test (no secrets): HTTP status codes, provider request IDs,
retry counts, latency (p50/p95), which credential served (by slot number,
not value), error bodies (truncated, key-checked).

Budget estimate: ~40-60 live requests total (L1-L8), 90% short prompts
("Reply with the single word: ok"). Worst case if caps hit: negligible
cost; all tests use minimal tokens.

## Pass criteria

1. L1-L4 pass; streaming integrity holds live (L2 has genuine terminal
   markers — no synthetic success).
2. L5/L6 establish per-key quota behavior with data, whichever way it
   falls: three independent pools → fleet-scale benefit is real; shared
   pool → gateway still correct but capacity argument collapses.
3. L7 shows live failover matches mock behavior (cooldown + shift).
4. Log redaction gate: 0 hits.
5. No test consumed unbounded retries; every failure was explicit.

## Explicitly out of scope (unchanged, HOLD)

- Production Hermes fleet migration.
- Changing Kanban concurrency controls.
- Multi-worker load beyond the L-matrix above.

## One-worker canary proposal (post-L approval, separate decision)

After L1-L8 pass and dasdot reviews the data: route exactly ONE Hermes
worker's traffic through the gateway for a working session; monitor
gateway logs for synthetic-success events (should be zero with the patch),
latency, and any 429 patterns; canary ends on any explicit streaming
failure or anomalous latency.
