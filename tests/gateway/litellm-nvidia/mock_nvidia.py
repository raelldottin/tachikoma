"""Mock NVIDIA NIM-compatible endpoint for LiteLLM gateway testing.

All credentials are fake test tokens (sk-...). No real API material is used
or accepted by this server; unknown bearer tokens receive 401.

Behavior is keyed off the Bearer token so each "API key" can be scripted
independently, exactly like distinct NVIDIA credentials:

  sk-good      -> 200 valid OpenAI-format chat completion
  sk-429       -> 429 (rate limited / quota)
  sk-revoked   -> 401 (credential rejected)
  sk-nomodel   -> 404 (model not available for this credential)
  sk-503       -> 503 (service unavailable)
  sk-streamcut -> streaming failure modes, selected via POST /control:
                    {"key": "sk-streamcut", "stream_mode": "<mode>"}
                  Modes:
                    clean_close    HTTP 200, SSE chunks, then valid chunked
                                  EOF with NO finish_reason and NO [DONE].
                                  (Reproduced synthetic-stop on 1.104.2.)
                    tcp_reset      Chunks then hard TCP RST (SO_LINGER 0).
                                  (Case fixed by upstream PR #33222.)
                    truncated_body Content-Length > bytes written, then RST.
                                  Should raise ClientPayloadError/ReadError.
                    ok            Fully valid stream incl. [DONE] (control).

Non-streaming requests always return a normal completion unless the key's
mode dictates an HTTP error.
"""

import json
import sys

from aiohttp import web

KEYS = {
    "sk-good": "ok",
    "sk-good2": "ok",
    "sk-429": "429",
    "sk-revoked": "401",
    "sk-nomodel": "404",
    "sk-503": "503",
    "sk-streamcut": "streamcut",
}

STATE = dict(KEYS)  # mutable copy
STREAM_MODES = {"clean_close", "tcp_reset", "truncated_body", "ok"}
STREAM_MODE = {"sk-streamcut": "clean_close"}

MODELS = ["z-ai/glm-5.3", "z-ai/glm-5.3-removed"]


def bearer(request):
    auth = request.headers.get("Authorization", "")
    return auth.replace("Bearer ", "").strip()


def sse_chunk(model, delta=None, finish_reason="__unset__"):
    choice = {"index": 0, "delta": delta or {}, "finish_reason": None}
    if finish_reason != "__unset__":
        choice["finish_reason"] = finish_reason
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": model,
        "choices": [choice],
    }


def chat_body(model):
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hello from mock"}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
    }


def _rst_socket(transport):
    """Hard TCP reset: abort() sends RST without a graceful FIN and is the
    aiohttp-supported way to kill the connection from the server side.
    (Do NOT close the underlying fd manually - aiohttp re-closes it later
    and a double close wedges the whole server.)"""
    if transport is None:
        return
    try:
        transport.abort()
    except Exception as e:
        print(f"[mock] rst_socket failed: {e}", flush=True)


async def chat_completions(request):
    key = bearer(request)
    model = "unknown"
    stream = False
    body: dict = {}
    try:
        body = await request.json()
        model = body.get("model", "unknown")
        stream = bool(body.get("stream", False))
    except Exception:
        pass
    mode = STATE.get(key, "unknown-key")
    print(f"[mock] key={key} mode={mode} model={model} stream={stream}", flush=True)

    if mode in ("unknown-key", "401"):
        return web.json_response({"error": {"message": "Unauthorized", "type": "auth_error"}}, status=401)
    if mode == "429":
        return web.json_response(
            {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}}, status=429
        )
    if mode == "404":
        return web.json_response(
            {"error": {"message": f"Model {model} not available", "type": "invalid_request_error"}}, status=404
        )
    if mode == "503":
        return web.json_response(
            {"error": {"message": "Service temporarily unavailable", "type": "server_error"}}, status=503
        )
    if model not in MODELS:
        return web.json_response(
            {"error": {"message": f"Model {model} not found", "type": "invalid_request_error"}}, status=404
        )

    if not stream:
        return web.json_response(chat_body(model))

    # ---- streaming paths ----
    smode = STREAM_MODE.get(key, "ok") if mode == "streamcut" else "ok"

    if smode == "truncated_body":
        # Announce Content-Length: 1000, write ~40 bytes, then RST.
        # Client should raise ClientPayloadError -> ReadError.
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Content-Length": "1000"})
        await resp.prepare(request)
        data = f"data: {json.dumps(sse_chunk(model, {'role': 'assistant'}))}\n\n".encode()
        await resp.write(data)
        _rst_socket(request.transport)
        return resp

    if smode == "tcp_reset":
        # One valid chunk, then hard RST. Client should raise ReadError.
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        data = f"data: {json.dumps(sse_chunk(model, {'role': 'assistant'}))}\n\n".encode()
        await resp.write(data)
        _rst_socket(request.transport)
        return resp

    if smode == "clean_close":
        # Valid chunked-encoding EOF but NO finish_reason chunk, NO [DONE].
        # Reproduced the synthetic-stop failure on LiteLLM 1.104.2.
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for delta in ({"role": "assistant"}, {"content": "Partial "},
                      {"content": "output that will nev"}):
            await resp.write(f"data: {json.dumps(sse_chunk(model, delta))}\n\n".encode())
        return resp  # clean EOF; aiohttp sends a valid chunked terminator

    # fully valid stream (control case)
    if body.get("tools"):
        # Valid TOOL-CALL stream: chunks carrying tool_calls deltas, then a
        # genuine finish_reason="tool_calls" + [DONE]. Guards the negative
        # control: the patch must reject incomplete streams without breaking
        # legitimate tool-call completions.
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        first = sse_chunk(model, {"role": "assistant"})
        first["choices"][0]["delta"]["tool_calls"] = [{
            "index": 0, "id": "call_mock_1", "type": "function",
            "function": {"name": "get_weather", "arguments": ""},
        }]
        second = sse_chunk(model, {})
        second["choices"][0]["delta"]["tool_calls"] = [{
            "index": 0, "function": {"arguments": '{"city": "Tokyo"}'},
        }]
        for payload in (first, second):
            await resp.write(f"data: {json.dumps(payload)}\n\n".encode())
        await resp.write(f"data: {json.dumps(sse_chunk(model, {}, finish_reason='tool_calls'))}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    for delta in ({"role": "assistant"}, {"content": "Hello "}, {"content": "from mock"}):
        await resp.write(f"data: {json.dumps(sse_chunk(model, delta))}\n\n".encode())
    await resp.write(f"data: {json.dumps(sse_chunk(model, {}, finish_reason='stop'))}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    return resp


async def control(request):
    body = await request.json()
    if "key" in body and "mode" in body:
        STATE[body["key"]] = body["mode"]
    if "key" in body and "stream_mode" in body:
        if body["stream_mode"] in STREAM_MODES:
            STREAM_MODE[body["key"]] = body["stream_mode"]
    print(f"[mock] control: {body}", flush=True)
    return web.json_response({"ok": True, "state": STATE, "stream_mode": STREAM_MODE})


app = web.Application()
app.router.add_post("/v1/chat/completions", chat_completions)
app.router.add_post("/control", control)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 43210
    web.run_app(app, host="127.0.0.1", port=port,
                print=lambda *a: print(f"[mock] listening on {port}", flush=True))
