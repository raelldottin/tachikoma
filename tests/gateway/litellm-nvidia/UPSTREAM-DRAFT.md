# Upstream Contribution Draft — for dasdot's review BEFORE publishing

Do not publish without approval. Two artifacts:

1. Comment on existing issue #40260 (do NOT open a duplicate — verified
   non-duplicate: #33404 = TCP-reset case only, closed by #33222; #40260 =
   clean-EOF case, open; #42085 = opt-in strict-mode feature request;
   #36768 = Responses API path).

2. Draft PR description for "fix: raise on stream EOF without a
   completion marker" (supersedes commenting-only; PR references #40260).

## Pre-publish checklist (self-verified 2026-10-10)

- [x] #40260 title/body confirm same root cause: StopAsyncIteration →
      _finalize_completed_stream() → finish_reason_handler() manufactures
      "stop"; reproduced on main d8c0e2c by the filer 2026-10-08.
- [x] Another user (poindexter12) reproduced on proxy 1.101.0 with stub
      upstreams; observed table across routes incl. /v1/messages synthetic
      anthropic finishes; OTEL span stays OK for clean EOF (vs ERROR for
      transport cuts). Matches our findings exactly.
- [x] Upstream main at streaming_handler.py:2116 still unguarded.
- [x] Our 16-line patch applies to main with zero conflicts? — VERIFIED
      LOCALLY ONLY at the same line context; the patch in the repo is
      generated against 1.104.2 (same hunk location, line 2074 in 1.104.2
      vs 2116 on main — the diff will need regeneration against main).
- [x] No credentials in the repro; all mock keys are synthetic.

## 1. Draft comment on #40260

---

Reproducing this with a full gateway-level differential on the LiteLLM
**proxy** (1.104.2), using a scripted mock upstream so the failure is
deterministic. Results confirm the root cause and show the transport-error
path fixed by #33222 works — the gap is exactly the `StopAsyncIteration`
branch.

**Test matrix** (all fixture credentials pinned to the failure mode so
router failover cannot mask the case; 4th/5th rows are controls):

| Upstream behavior | LiteLLM 1.104.2 stock | With guard |
|---|---|---|
| TCP reset mid-stream (aiohttp `transport.abort()`) | HTTP 500, explicit failure | HTTP 500, explicit failure |
| Chunked body truncated before terminator (Content-Length/bytes mismatch) | HTTP 500, explicit failure | HTTP 500, explicit failure |
| **Clean chunked-encoding EOF after 2 deltas, no `finish_reason`, no `[DONE]`** | **HTTP 200, synthetic `finish_reason:"stop"` + `[DONE]`** | **HTTP 200, stream ends WITHOUT any terminal marker — incomplete, distinguishable** |
| Valid stream (control) | 200 with genuine `[DONE]` | 200 with genuine `[DONE]` |
| Valid tool-call stream (control) | 200, `finish_reason:"tool_calls"` | 200, `finish_reason:"tool_calls"` |

Raw SSE capture of the failing case (stock 1.104.2):

```text
data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","model":"nvidia-primary","choices":[{"index":0,"delta":{"role":"assistant"}}]}

data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","model":"nvidia-primary","choices":[{"index":0,"delta":{"content":"Partial "}}]}

data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","model":"nvidia-primary","choices":[{"index":0,"delta":{"content":"output that will nev"}}]}

data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","model":"nvidia-primary","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

The last two SSE events are manufactured by the gateway. The upstream sent
only the first three.

**Proposed minimal fix** (same shape as the #33222 transport guard, now
applied to the StopAsyncIteration branch):

```python
except (StopAsyncIteration, StopIteration):
    # A stream that ends without the upstream ever sending a completion
    # marker is a truncated response, not a success. Without this guard
    # the gateway synthesizes finish_reason="stop" + [DONE] downstream.
    # (Related: #33404 fixed the transport-error path; #42085 proposes an
    # opt-in strict mode; this fixes the default path.)
    if self.received_finish_reason is None:
        self._log_stream_failure_and_raise(
            RuntimeError(
                "LiteLLM stream ended without a completion marker "
                "(no finish_reason received before upstream EOF); "
                "refusing to synthesize a successful completion"
            )
        )
    return await self._finalize_completed_stream(cache_hit=cache_hit)
```

Note the patch deliberately does **not** change behavior for streams that
DID receive a finish_reason but lack `[DONE]` (some providers omit the
sentinel) — those continue to finalize normally.

**Downstream behavior with the guard (captured, LiteLLM proxy 1.104.2):**
once the 200 stream has begun the status cannot retroactively change, so
the raise is converted by the proxy's stream generator into an explicit
SSE error event — genuine chunks forward, then:

```text
data: {"error": {"message": "LiteLLM stream ended without a completion
marker (no finish_reason received before upstream EOF); refusing to
synthesize a successful completion", "type": "...", "code": "500"}}
```

No synthetic `finish_reason:"stop"` chunk, no `[DONE]`. This matches
LiteLLM's documented mid-stream error mechanism and preserves the partial
output for diagnostics, per the issue's expected behavior. Legitimate
completion reasons — `"length"`, `"content_filter"`, `"tool_calls"` — are
untouched: the guard only fires when NO terminal event ever arrived, and
the valid tool-call control case (`finish_reason:"tool_calls"`) passes on
both stock and patched builds. The guard does not retry: a partially
delivered response is surfaced as a failure, not replayed.

**Why fail rather than an opt-in strict mode (#42085):** a truncated
completion is indistinguishable from a good one by any downstream check
requiring finish_reason, because the gateway itself manufactures the
marker. Chat-completions consumers generally cannot opt in — they see
HTTP 200 + a well-formed terminal event. The choice of "stop" also leaks
into billing/spend tracking and OTEL spans as a success (as @poindexter12
observed, the OTEL span stays OK for clean EOF). For an agent runtime this
means a silently truncated long completion is recorded as complete and
the conversation continues with a missing answer.

We've shipped this guard locally (16 lines) with a regression suite:
stock fails case 3, patched passes 5/5 including tool-call controls.
Happy to open a PR with the fix + the differential regression tests if
maintainers agree this should fail by default.

## 2. Draft PR description

---

**Title:** fix(streaming): raise on clean upstream EOF without a completion marker

**Refs:** fixes #40260 (partial — default behavior for chat completions);
relationship to #33404 (transport errors, fixed by #33222): this PR
covers the adjacent `StopAsyncIteration` path left unguarded by that fix.

### Problem

`CustomStreamWrapper.__anext__` guards the transport-error branch
(`httpx.ReadError` / `RemoteProtocolError`, from #33222) with a
`received_finish_reason is None` check, but the `StopAsyncIteration`
branch — hit when the upstream HTTP response ends cleanly without ever
sending `finish_reason` / `[DONE]` — calls
`_finalize_completed_stream()` unconditionally. `finish_reason_handler()`
then synthesizes a `finish_reason="stop"` chunk and emits `[DONE]`, so a
truncated response is reported to clients, spend tracking, and OTEL as a
successful completion.

### Reproduction (deterministic, no provider credentials)

Scripted mock upstream (aiohttp) serving `/v1/chat/completions` with
`stream: true`: two content deltas, then a clean chunked-encoding
terminator — no finish chunk, no `[DONE]`.

```python
# upstream handler (essence)
resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
await resp.prepare(request)
await resp.write(b'data: {"choices":[{"delta":{"content":"Partial "}}]}\n\n')
await resp.write(b'data: {"choices":[{"delta":{"content":"output ..."}}]}\n\n')
return resp  # clean EOF: valid terminator, no completion marker
```

Observed downstream of LiteLLM proxy 1.104.2 (and on current main):

```text
data: {"choices":[{"delta":{},"finish_reason":"stop"}]}   <- synthesized
data: [DONE]                                              <- synthesized
```

### Fix

Mirror the existing #33222 transport guard in the StopAsyncIteration
branch: if no completion marker was received before EOF, raise instead of
finalizing. Streams that DID receive `finish_reason` (with or without a
literal `[DONE]` sentinel — providers vary) are unaffected.

### Tests

Differential regression suite (mock upstream, no credentials needed):

1. TCP reset mid-stream → explicit failure (regression: #33222 path)
2. Truncated chunked body → explicit failure
3. Clean EOF without marker → explicit failure (this fix)
4. Valid stream → success (control)
5. Valid tool-call stream (`finish_reason="tool_calls"`) → success (control)

Stock 1.104.2: 1, 2, 4, 5 pass; 3 fails silently. Patched: 5/5.

## 3. Notes for dasdot

- #40260's filer reproduced via direct SDK (no proxy); poindexter12 via
  proxy with stub upstreams; our gateway-level differential matches both.
- Their reproduction and ours agree the bug is on main today (verified
  2026-10-08 and 2026-10-10 respectively).
- #42085 (opt-in strict mode) covers the same symptom differently; our
  position in the comment: default-fail is the correct default because
  downstream consumers can't opt in to a check the gateway itself defeats.
- Wait for maintainer response (leilei3167 commented "looking at this"
  2026-09-08) before opening the PR, or open immediately referencing
  #40260 — your call.
