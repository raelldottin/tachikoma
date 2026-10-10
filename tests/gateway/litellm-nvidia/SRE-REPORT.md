# SRE Report — LiteLLM 1.104.2 synthetic-success on upstream clean EOF

**Status:** Resolved locally; upstream gap confirmed unfixed on `main`.

## Impact

Any client streaming through the LiteLLM gateway can receive an
apparently-successful but truncated completion when the upstream provider
closes the HTTP response cleanly without sending a completion marker
(`finish_reason` chunk / `[DONE]` sentinel). The gateway synthesizes
`finish_reason="stop"` and `[DONE]`, presenting an incomplete generation as
success. Downstream consumers (e.g. Hermes agents) cannot detect the loss;
a client-side "require finish_reason" check is defeated because the gateway
itself manufactures the marker.

Observed with z-ai/glm-5.3 via NVIDIA NIM in production Hermes traffic
(premature response termination) and reproduced deterministically on the
mock harness.

## Scope

- LiteLLM 1.104.2 proxy, `/v1/chat/completions` with `stream: true`
- Any OpenAI-compatible upstream; not NVIDIA-specific
- Not the Responses API path (upstream issue #36768 covers that separately)
- Non-streaming requests unaffected

## Recent Change

- Upstream PR #33222 (merged July 2026, present in 1.104.2) fixed the
  *transport-error* path: `httpx.ReadError` / `RemoteProtocolError` with
  `received_finish_reason is None` now raises instead of finalizing.
- The `StopAsyncIteration` path (clean EOF) was never given the same guard.

## Evidence

Differential harness: `test_streaming_integrity.py` against
`mock_nvidia.py` (deterministic, zero inference credits), LiteLLM 1.104.2,
all fixture credentials pinned per case so router failover cannot mask
results.

| Case | Mode | Stock 1.104.2 | Patched |
|---|---|---|---|
| A | TCP reset mid-stream | explicit failure (500) — #33222 works | explicit failure (500) |
| B | Body shorter than Content-Length | explicit failure (500) | explicit failure (500) |
| C | **Clean chunked EOF, no marker** | **silent success: synthetic `finish_reason:"stop"` + `[DONE]`** | **detected: stream ends without `[DONE]`, no synthetic marker** |
| D | Fully valid stream (control) | success with genuine `[DONE]` | unchanged |

Raw client-side SSE for case C on stock 1.104.2 (verbatim):

```
data: {"...","choices":[{"index":0,"delta":{"role":"assistant"}}]}
data: {"...","choices":[{"index":0,"delta":{"content":"Partial "}}]}
data: {"...","choices":[{"index":0,"delta":{"content":"output that will nev"}}]}
data: {"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

The upstream never sent the last two frames.

Source evidence (installed 1.104.2, `litellm_core_utils/streaming_handler.py`):
- `except (httpx.ReadError, httpx.RemoteProtocolError)`: raises when
  `self.received_finish_reason is None` — the #33222 guard.
- `except (StopAsyncIteration, StopIteration)`:
  `return await self._finalize_completed_stream(...)` unconditionally —
  `_finalize_completed_stream` calls `finish_reason_handler()` which
  manufactures the marker when none was received.
- LiteLLM `main` (2026-10-10) still contains the identical unguarded branch.

## Hypotheses

1. ~~Regression of #33222 in 1.104.2~~ — refuted: transport-error cases A/B
   fail explicitly, proving the fix is present and active.
2. ~~Test error (mock ends stream in a way no real provider would)~~ —
   refuted as a dismissal: clean EOF without a marker is exactly what a
   provider-side timeout/kill after partial generation looks like on the
   wire; we have production evidence of truncated glm-5.3 output.
3. **Different, unfixed failure path (confirmed):** clean EOF raises
   `StopAsyncIteration`, not a transport error; that branch lacks the
   `received_finish_reason` guard.

## Tests

See `test_streaming_integrity.py` (cases A–D). Run order:

```
# mock upstream
python mock_nvidia.py 43210
# gateway (venv with litellm[proxy]==1.104.2)
export NVIDIA_KEY_1=sk-good NVIDIA_KEY_2=sk-429 NVIDIA_KEY_3=sk-streamcut \
       GATEWAY_MASTER_KEY=sk-gateway-test-key
litellm --config config.yaml --port 4000 --host 127.0.0.1
python test_streaming_integrity.py          # 4/4 with patch, 3/4 stock (C fails)
python run_acceptance.py                    # 10/10 with patch (incl. restart)
```

Regression safety: full acceptance suite (credential balancing, 429
failover, revoked/404 exclusion, quota exhaustion, bounded 503 retries,
model-disappearance, concurrency, restart survival, log redaction) passes
10/10 on the patched gateway; post-restart 200s 10/10; `proxy.log` key
redaction 0 hits.

## Mitigation

`streaming_handler_clean-EOF.patch` — 16-line guard in the
`StopAsyncIteration` branch mirroring the existing #33222 check:
if `received_finish_reason is None` at clean upstream EOF,
`_log_stream_failure_and_raise(RuntimeError(...))`. No fork: apply with

```
cd $(python -c 'import litellm,os;print(os.path.dirname(litellm.__file__))')
patch -p1 < .../streaming_handler_clean-EOF.patch
```

(re-apply after any `pip install -U litellm`).

## Root Cause

LiteLLM's `CustomStreamWrapper.__anext__` treats *any* clean end of the
upstream iterator as a completed stream. `_finalize_completed_stream`
→ `finish_reason_handler()` fills in a missing `finish_reason` and the proxy
emits `[DONE]`. The #33222 fix guarded only the exception path
(`httpx.ReadError`/`RemoteProtocolError`), not the `StopAsyncIteration`
path, so a provider that closes the connection gracefully mid-generation
still yields a synthetic success.

## Prevention

- Keep `test_streaming_integrity.py` as a regression gate for any LiteLLM
  upgrade; case C is the canary.
- File an upstream issue referencing #33404 / #36768 with this differential
  repro (mock harness included here, no credentials, zero inference cost).
- Do not accept a downstream `finish_reason="stop"` as proof of upstream
  completion in Hermes-side checks; the marker must come from the provider.
- Fleet rollout stays HOLD until streaming integrity is fixed upstream or
  the local patch is pinned in deployment automation.
