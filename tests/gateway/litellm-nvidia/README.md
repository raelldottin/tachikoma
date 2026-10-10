# LiteLLM ↔ NVIDIA NIM gateway test harness

Deterministic regression suite for a LiteLLM proxy fronting NVIDIA NIM
(z-ai/glm-5.3). Reproduces provider failure modes — 401/404/429/503, TCP
reset mid-stream, truncated HTTP body, clean EOF without completion
markers — with **zero inference credits**: all tests run against the local
mock in `mock_nvidia.py`, never the real NVIDIA API.

## Files

- `mock_nvidia.py` — mock NVIDIA NIM endpoint (:43210). Behavior keyed off
  fake Bearer tokens (`sk-good`, `sk-429`, …); `POST /control` reconfigures
  keys at runtime. Stream modes: `clean_close`, `tcp_reset`,
  `truncated_body`, `ok`.
- `config.yaml` — sanitized gateway config. Master key comes from
  `GATEWAY_MASTER_KEY`; NVIDIA keys from `NVIDIA_KEY_1..3`. **No credentials
  in this file; nothing key-shaped gets committed.**
- `run_acceptance.py` — 11-scenario credential/routing acceptance suite
  (dasdot's §4 table): balancing, 429 failover, revoked/404 exclusion,
  quota exhaustion, bounded 503 retries, model disappearance, concurrency,
  restart survival (caller-driven), streaming-integrity canary, log
  redaction.
- `test_streaming_integrity.py` — 4-case differential streaming suite
  (A tcp_reset, B truncated_body, C clean_close, D control). **Case C is
  the canary for the synthetic-success bug** — see `SRE-REPORT.md`.
- `streaming_handler_clean-EOF.patch` — local LiteLLM 1.104.2 patch; guards
  the `StopAsyncIteration` branch the same way PR #33222 guards transport
  errors. Re-apply after every LiteLLM upgrade and re-run both suites.

## Running

```bash
# venv with litellm[proxy]==1.104.2
python mock_nvidia.py 43210 &
export NVIDIA_KEY_1=sk-good NVIDIA_KEY_2=sk-429 NVIDIA_KEY_3=sk-revoked
export GATEWAY_MASTER_KEY=sk-gateway-test-key
litellm --config config.yaml --port 4000 --host 127.0.0.1 &
python test_streaming_integrity.py   # expect 4/4 (patched) / 3/4 (stock)
python run_acceptance.py             # expect 10/10 (patched)
```

## Expected results

- **Stock LiteLLM 1.104.2:** streaming C fails — gateway synthesizes
  `finish_reason:"stop"` + `[DONE]` over a truncated upstream stream
  (silent success). A, B pass (PR #33222 works). Acceptance 9/10.
- **With patch:** streaming 4/4; acceptance 10/10.

## Hygiene

- Fake `sk-*` tokens only. Never put real NVIDIA keys, `.env`, auth
  headers, or raw logs (mock.log prints keys by design) under version
  control.
- Upgrade procedure: `pip install -U litellm` → re-apply patch → run both
  suites → if upstream has fixed case C natively, drop the patch.
