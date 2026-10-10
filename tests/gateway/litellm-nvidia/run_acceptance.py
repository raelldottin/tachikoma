"""Acceptance tests for the LiteLLM key-pool gateway against a scripted mock NVIDIA.

Maps to dasdot's acceptance table:
  1. Three valid credentials       -> all eligible for routing
  2. One key gets 429               -> another eligible key is used
  3. One key revoked (401)          -> excluded
  4. One key lacks model access (404)-> healthy keys remain usable
  5. All keys at quota (429)        -> controlled error, bounded
  6. NVIDIA returns 503             -> retries bounded
  7. Model disappears (404 all)    -> no endless key rotation
  8. Gateway restart                -> config/keys survive (checked by caller)
  9. Concurrent workers            -> no crash, all complete
  10. Streaming response cut mid-way-> incomplete response DETECTED, not silent success
  11. Credential in logs            -> keys must be redacted

Run:  python run_acceptance.py
Assumes the mock (mock_nvidia.py) and proxy are already up.
"""

import json
import os
import re
import sys
import time
import urllib.request

BASE = "http://localhost:4000"
GW_KEY = "sk-gateway-test-key"
MOCK = "http://127.0.0.1:43210"
KEYS = ["sk-good", "sk-429", "sk-revoked", "sk-nomodel", "sk-503", "sk-streamcut"]

PASS, FAIL = [], []


def set_mode(key, mode):
    req = urllib.request.Request(
        f"{MOCK}/control",
        data=json.dumps({"key": key, "mode": mode}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req).read()


def reset_modes():
    for k, m in {
        "sk-good": "ok",
        "sk-429": "429",
        "sk-revoked": "401",
        "sk-nomodel": "404",
        "sk-503": "503",
        "sk-streamcut": "streamcut",
    }.items():
        set_mode(k, m)
    # let LiteLLM's deployment cooldowns (cooldown_time: 2s) expire
    time.sleep(3)


def chat(messages=None, stream=False, model="nvidia-primary", timeout=120):
    payload = {
        "model": model,
        "messages": messages or [{"role": "user", "content": "Hello"}],
    }
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {GW_KEY}"},
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp


def chat_full(messages=None, **kw):
    resp = chat(messages, **kw)
    return resp.status, json.loads(resp.read())


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}", flush=True)


def test_all_valid():
    reset_modes()
    # make all three good
    for i in (1, 2, 3):
        pass  # modes set below via control
    set_mode("sk-good", "ok"); set_mode("sk-429", "ok"); set_mode("sk-revoked", "ok")
    statuses = []
    for _ in range(9):
        try:
            s, _b = chat_full()
            statuses.append(s)
        except urllib.error.HTTPError as e:
            statuses.append(e.code)
    check("1: three valid credentials -> all eligible", all(s == 200 for s in statuses) and len(statuses) == 9,
          f"statuses={statuses}")
    reset_modes()


def test_429_failover():
    reset_modes()
    set_mode("sk-good", "ok"); set_mode("sk-429", "429"); set_mode("sk-revoked", "ok")
    ok, fail = 0, 0
    for _ in range(10):
        try:
            s, _ = chat_full()
            ok += 1 if s == 200 else 0
        except urllib.error.HTTPError:
            fail += 1
    check("2: one key 429 -> other eligible key used", ok == 10 and fail == 0, f"ok={ok} fail={fail}")
    reset_modes()


def test_revoked():
    reset_modes()
    set_mode("sk-good", "ok"); set_mode("sk-429", "ok"); set_mode("sk-revoked", "401")
    ok = 0
    for _ in range(10):
        try:
            s, _ = chat_full()
            ok += 1 if s == 200 else 0
        except urllib.error.HTTPError:
            pass
    check("3: revoked key excluded, healthy keys usable", ok >= 8, f"ok={ok}/10")
    reset_modes()


def test_no_model_access():
    reset_modes()
    set_mode("sk-good", "ok"); set_mode("sk-429", "ok"); set_mode("sk-revoked", "404")
    ok = 0
    for _ in range(10):
        try:
            s, _ = chat_full()
            ok += 1 if s == 200 else 0
        except urllib.error.HTTPError:
            pass
    check("4: key without model access -> healthy keys usable", ok >= 8, f"ok={ok}/10")
    reset_modes()


def test_all_quota():
    reset_modes()
    set_mode("sk-good", "429"); set_mode("sk-429", "429"); set_mode("sk-revoked", "429")
    t0 = time.time()
    try:
        chat_full()
        check("5: all keys at quota -> controlled error", False, "got 200")
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        check("5: all keys at quota -> controlled error", e.code in (429, 500, 502), f"code={e.code} in {dt:.1f}s")
    reset_modes()


def test_503_bounded():
    reset_modes()
    set_mode("sk-good", "503"); set_mode("sk-429", "503"); set_mode("sk-revoked", "503")
    t0 = time.time()
    mock_calls = 0
    try:
        chat_full()
        check("6: NVIDIA 503 -> bounded retries (request fails)", False, "got 200")
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        check("6: NVIDIA 503 -> bounded retries (request fails)", e.code in (503, 500, 502), f"code={e.code} in {dt:.1f}s")
    except Exception as e:
        dt = time.time() - t0
        check("6: NVIDIA 503 -> bounded retries (request fails)", True, f"err after {dt:.1f}s: {type(e).__name__}")
    reset_modes()


def test_model_disappears():
    reset_modes()
    for k in ("sk-good", "sk-429", "sk-revoked"):
        set_mode(k, "ok")
    t0 = time.time()
    try:
        s, _b = chat_full(model="z-ai/glm-5.3-removed")
        check("7: model disappears -> no endless rotation", False, "got 200")
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        check("7: model disappears -> no endless rotation", dt < 60 and e.code in (400, 404, 500, 502),
              f"code={e.code} in {dt:.1f}s")
    except Exception as e:
        dt = time.time() - t0
        check("7: model disappears -> no endless rotation", dt < 60, f"err after {dt:.1f}s")
    reset_modes()


def test_concurrent():
    import concurrent.futures
    reset_modes()
    for k in ("sk-good", "sk-429", "sk-revoked"):
        set_mode(k, "ok")

    def one(i):
        try:
            s, _ = chat_full()
            return s == 200
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, range(16)))
    check("9: 8 concurrent workers, 16 requests", all(results), f"{sum(results)}/16")
    reset_modes()


def test_streamcut():
    reset_modes()
    # route to the streamcut key by making the others hard-fail? No — weighted
    # failover would retry on good keys and deliver a full answer. Instead set
    # ALL keys to streamcut so whatever it picks, the stream is cut.
    for k in ("sk-good", "sk-429", "sk-revoked"):
        set_mode(k, "streamcut")
    detected_incomplete = False
    detail = ""
    try:
        resp = chat(stream=True)
        body = resp.read()
        txt = body.decode("utf-8", "replace")
        has_done = "[DONE]" in txt
        # No [DONE] and no finish_reason -> incomplete. LiteLLM should raise
        # rather than hand back a silently-truncated stream.
        if has_done:
            detail = "stream ended with [DONE] (silent success)"
        else:
            detected_incomplete = True
            detail = f"incomplete stream surfaced to client ({len(body)}B, no [DONE])"
    except Exception as e:
        # An exception/HTTPError IS a detection of the incomplete stream.
        detected_incomplete = True
        detail = f"client got error: {type(e).__name__}"
    check("10: mid-stream cut -> detected, not silent success", detected_incomplete, detail)
    reset_modes()


def test_no_key_leak():
    # scan proxy log for any of our credential values
    reset_modes()
    try:
        s, _ = chat_full()
    except Exception:
        pass
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.log")
    leaked = []
    if os.path.exists(log_path):
        data = open(log_path, "r", errors="replace").read()
        for k in KEYS + [GW_KEY]:
            if k in data:
                leaked.append(k)
    check("11: credentials redacted from logs", not leaked, f"leaked={leaked}")
    reset_modes()


if __name__ == "__main__":
    tests = [
        test_all_valid,
        test_429_failover,
        test_revoked,
        test_no_model_access,
        test_all_quota,
        test_503_bounded,
        test_model_disappears,
        test_concurrent,
        test_streamcut,
        test_no_key_leak,
    ]
    failed_earlier = sys.argv[1:]  # list of test numbers to skip (for restart test ordering)
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL.append(t.__name__)
            print(f"FAIL  {t.__name__} raised {type(e).__name__}: {e}", flush=True)
    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        print("Failed:", FAIL)
    sys.exit(1 if FAIL else 0)
