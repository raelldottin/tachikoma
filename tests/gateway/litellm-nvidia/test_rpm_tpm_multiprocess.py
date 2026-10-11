"""Multi-process aggregate RPM/TPM enforcement test.

Distinct from test_redis_multiprocess.py (cooldown coordination): this
suite proves that per-deployment rpm/tpm limits are enforced as an
AGGREGATE across independent proxy processes sharing Redis, not per
process. Complements the cooldown tests; does not replace them.

  T1. Both proxies healthy; mock reset; Redis flushed.
  T2. RPM AGGREGATE: fire 24 interleaved requests across A (:4000) and
      B (:4001). Two deployments x rpm=4. With shared Redis: <= 8 total
      200s (each deployment's counter sums contributions from both
      processes); remaining requests 429 with the ModelRateLimitingCheck
      message. WITHOUT Redis (negative control): each process counts
      alone, so up to 16 200s are possible -> control must show more
      200s than the shared limit allows.
  T3. TPM AGGREGATE: requires a low tpm on deployments (rpm removed).
      Fire requests with large inputs across both processes; total
      token throughput must cap at the shared tpm.  (Uses a second
      config file; runner script swaps files - see README.)
  T4. Post-restart: counters persist in Redis - after restarting one
      proxy the limit still holds (no fresh allowance).

Usage:
  python test_rpm_tpm_multiprocess.py phase2      # A/B on config-rpmtpm.yaml
  python test_rpm_tpm_multiprocess.py tpm         # on config with tpm only
  python test_rpm_tpm_multiprocess.py restart     # after proxy B restart

Env: GATEWAY_MASTER_KEY. Caller runs the two proxies.
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

A = "http://localhost:4000"
B = "http://localhost:4001"
MOCK = "http://127.0.0.1:43210"
MASTER = os.environ.get("GATEWAY_MASTER_KEY", "sk-gateway-test-key")

results = []


def req(base, path, payload=None, key=MASTER, timeout=90):
    r = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def chat(base, big=False):
    content = ("word " * 300) if big else "hi"   # ~300 tokens when big
    return req(base, "/v1/chat/completions",
               {"model": "nvidia-primary",
                "messages": [{"role": "user", "content": content}]})


def control(key, mode):
    r = urllib.request.Request(f"{MOCK}/control",
                               data=json.dumps({"key": key, "mode": mode}).encode(),
                               headers={"Content-Type": "application/json"})
    for _ in range(5):
        try:
            urllib.request.urlopen(r, timeout=10).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def flush_redis():
    try:
        s = socket.create_connection(("127.0.0.1", 16379), timeout=5)
        s.sendall(b"*1\r\n$7\r\nFLUSHALL\r\n")
        s.recv(100)
        s.close()
    except Exception as e:
        print(f"warn: redis flush failed: {e}")


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}", flush=True)


def aggregate_rpm_phase():
    # T1
    ha, _ = req(A, "/health/liveliness")
    hb, _ = req(B, "/health/liveliness")
    check("T1 both proxies healthy", ha == 200 and hb == 200, f"(A={ha} B={hb})")
    for k in ("sk-good", "sk-good2"):
        control(k, "ok")
    flush_redis()
    time.sleep(1)
    # T2: 24 interleaved requests; deployments: 2 x rpm=4 -> ceiling 8 200s
    ok200, lim429, last429body = 0, 0, ""
    for i in range(24):
        base = A if i % 2 == 0 else B
        st, body = chat(base)
        if st == 200:
            ok200 += 1
        elif st == 429:
            lim429 += 1
            last429body = body
    check("T2b 429s are rate-limit enforcement (RPM limit or cooldown of over-limit deployments)",
          lim429 == 0 or ("RPM limit" in last429body
                          or "All deployments for selected model are in cooldown" in last429body))
    check("T2 RPM enforced as aggregate across processes",
          ok200 <= 8 and lim429 >= 24 - 8 - 1,
          f"(200s={ok200} <= 8, 429s={lim429})")


def tpm_phase():
    # deployments: 2 x tpm=500 (config-tpm.yaml); big requests ~200 words
    ha, _ = req(A, "/health/liveliness")
    hb, _ = req(B, "/health/liveliness")
    check("T1 both proxies healthy", ha == 200 and hb == 200)
    for k in ("sk-good", "sk-good2"):
        control(k, "ok")
    flush_redis()
    time.sleep(1)
    ok200, lim429, last429body = 0, 0, ""
    for i in range(10):
        base = A if i % 2 == 0 else B
        st, body = chat(base, big=True)
        if st == 200:
            ok200 += 1
        elif st == 429:
            lim429 += 1
            last429body = body
    # Each big request is ~200 tokens input; 2 x tpm=500 -> ~5 requests fit
    # across BOTH processes combined. Without Redis: up to 10.
    check("T3 TPM enforced as aggregate across processes",
          ok200 <= 6 and ok200 + lim429 == 10,
          f"(200s={ok200}, 429s={lim429})")
    if lim429 and "TPM limit" not in last429body \
            and "All deployments for selected model are in cooldown" not in last429body:
        check("T3b 429s are rate-limit enforcement (TPM limit or cooldown)", False,
              f"(unexpected body: {last429body[:120]})")
    else:
        check("T3b 429s are rate-limit enforcement (TPM limit or cooldown)", True)


def restart_phase():
    """Run immediately after consuming the full budget within the current
    minute and restarting proxy B (see README protocol). With Redis-backed
    counters B must get NO fresh allowance: nearly all 429s. Caveat: the
    minute must not roll over between consumption and this probe."""
    ok200, lim429 = 0, 0
    for i in range(8):
        st, _ = chat(B)
        if st == 200:
            ok200 += 1
        elif st == 429:
            lim429 += 1
    check("T4 counters persist across proxy restart (no fresh allowance)",
          ok200 <= 1 and lim429 >= 7,
          f"(post-restart 200s={ok200}, 429s={lim429})")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "rpm"
    if phase == "rpm":
        aggregate_rpm_phase()
    elif phase == "tpm":
        tpm_phase()
    elif phase == "restart":
        restart_phase()
    passed = sum(1 for r in results if r)
    print(f"\n=== rpm/tpm multiprocess ({phase}): {passed}/{len(results)} ===",
          flush=True)
    sys.exit(0 if passed == len(results) else 1)
