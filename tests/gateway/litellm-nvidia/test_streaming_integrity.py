"""Streaming integrity acceptance suite for the LiteLLM gateway.

Four differential cases (dasdot's table):
  A. tcp_reset       - upstream TCP reset mid-stream      -> MUST fail
  B. truncated_body  - HTTP body shorter than announced    -> MUST fail
  C. clean_close     - valid EOF, no completion marker    -> MUST fail
  D. ok (control)    - fully valid stream                  -> MUST succeed

Verdict per case: does the CLIENT see an explicit failure (HTTP error,
exception mid-stream, or stream terminated WITHOUT a [DONE]/finish marker),
or an apparently-successful-but-possibly-incomplete response?

"Silent success" = client receives finish_reason="stop" and/or [DONE] even
though the upstream never sent a completion marker. That is the bug class
from LiteLLM issue #33404.

Run:
    python run_acceptance.py --streaming-only
Requires: mock_nvidia.py on :43210 and litellm proxy on :4000 with
NVIDIA_KEY_*=sk-good|sk-429|sk-revoked, master key sk-gateway-test-key.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:4000"
GW_KEY = "sk-gateway-test-key"
MOCK = "http://127.0.0.1:43210"

RESULTS = []


def control(payload):
    req = urllib.request.Request(
        f"{MOCK}/control", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    # RST-ing sockets can also reset our control connection; retry briefly.
    import time as _time
    last: Exception = RuntimeError("control: no attempt executed")
    for _ in range(5):
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return
        except Exception as e:
            last = e
            _time.sleep(0.5)
    raise last


def stream_request():
    """Send a streaming chat request; read the SSE body to completion.

    Returns dict(status, body, error). error is set if an exception was
    raised while reading (which counts as EXPLICIT FAILURE for our purposes).
    """
    payload = {"model": "nvidia-primary", "stream": True,
               "messages": [{"role": "user", "content": "Hello"}]}
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {GW_KEY}"})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        status = resp.status
        body = resp.read().decode("utf-8", "replace")
        return {"status": status, "body": body, "error": None}
    except urllib.error.HTTPError as e:
        # error surfaced before/without a 200 stream
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return {"status": e.code, "body": detail, "error": f"HTTPError {e.code}"}
    except Exception as e:
        # mid-stream transport exception from the gateway = explicit failure
        return {"status": None, "body": "", "error": f"{type(e).__name__}: {e}"}


def classify(result):
    """Classify gateway behavior for one streaming request.

    Returns (verdict, detail) where verdict is one of:
      explicit_failure - HTTP error status, or read exception
      silent_success   - 200 stream terminated with [DONE] (may be synthetic)
      truncated_stream - 200 stream ended WITHOUT [DONE] (incomplete surfaced)
    """
    if result["error"]:
        return "explicit_failure", result["error"]
    if result["status"] != 200:
        return "explicit_failure", f"HTTP {result['status']}"
    body = result["body"]
    if "[DONE]" in body:
        return "silent_success", "stream ended with [DONE]"
    return "truncated_stream", "200 stream ended without [DONE]"


ALL_KEYS = ["sk-good", "sk-429", "sk-revoked", "sk-nomodel", "sk-503", "sk-streamcut"]
# default (healthy) mode per key, matching mock_nvidia.py's KEYS map
KEYS_DEFAULT = {
    "sk-good": "ok",
    "sk-429": "429",
    "sk-revoked": "401",
    "sk-nomodel": "404",
    "sk-503": "503",
    "sk-streamcut": "streamcut",
}


def run_case(name, mode, must):
    # Pin ALL fixture credentials to the failure mode: whichever deployment
    # the router picks, it must hit the case under test (no healthy key can
    # recover and mask the result). For the control case, restore defaults.
    for k in ALL_KEYS:
        if mode == "ok":
            control({"key": k, "mode": KEYS_DEFAULT[k]})
        else:
            control({"key": k, "mode": "streamcut", "stream_mode": mode})
    result = stream_request()
    verdict, detail = classify(result)
    # For failure cases, truncated_stream also counts as detected (the
    # incompleteness is visible to the client rather than papered over).
    ok = (verdict == must) or (must == "explicit_failure" and verdict == "truncated_stream")
    line = f"{'PASS' if ok else 'FAIL'}  {name:16s} expect={must:17s} got={verdict:17s} ({detail[:90]})"
    print(line, flush=True)
    RESULTS.append((name, mode, must, verdict, ok, detail, result.get("body", "")))
    return ok


if __name__ == "__main__":
    cases = [
        ("A tcp_reset",      "tcp_reset",      "explicit_failure"),
        ("B truncated_body", "truncated_body", "explicit_failure"),
        ("C clean_close",    "clean_close",    "explicit_failure"),
        ("D control_ok",      "ok",             "silent_success"),  # genuine [DONE] is required here
    ]
    passed = 0
    for name, mode, must in cases:
        if run_case(name, mode, must):
            passed += 1
    print(f"\n=== streaming integrity: {passed}/{len(cases)} ===", flush=True)

    # Extra: dump the raw client-visible SSE for the clean_close case so the
    # synthetic finish_reason (if any) is captured verbatim in the report.
    if any(not r[4] for r in RESULTS):
        print("\n--- raw SSE from failing cases ---", flush=True)
        for name, mode, must, verdict, ok, detail, body in RESULTS:
            if not ok:
                print(f"### {name} (mode={mode}) -> {verdict}\n{body[:1200]}\n", flush=True)

    sys.exit(0 if passed == len(cases) else 1)
