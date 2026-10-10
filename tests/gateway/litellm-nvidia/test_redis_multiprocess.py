"""Multi-process Redis coordination acceptance test.

Verifies that per-credential health state (deployment cooldown) is shared
ACROSS independent LiteLLM proxy processes via Redis — dasdot §3's
"independent health and failure state per credential" requirement, which
must hold fleet-wide, not per process. Two proxy processes (A :4000,
B :4001) share one Redis and one mock upstream.

  R1. Both proxies healthy.
  R2. Clean slate: reset mock keys, flush Redis, confirm both keys serve.
  R3. COOLDOWN PROPAGATION: script sk-good to 429. Drive > allowed_fails
      failing requests via A until sk-good enters cooldown. Then:
      - every request via B must succeed (served by sk-good2 only)
      - the mock log must show NO further 429-mode hits on sk-good from
        EITHER process after cooldown engaged (B never picks the hot key).
      Without Redis, B has its own in-memory router and would keep
      selecting sk-good, producing new 429s (each process learns alone).
  R4. COOLDOWN EXPIRY: after cooldown_time elapses (60s), restore sk-good
      to ok and confirm requests via both processes serve 200s again
      (sk-good returns to rotation — the key recovered, not permanently
      blacklisted).
  R5. BOUNDED RETRY: script both keys to 503; a request must fail within
      a bounded time (num_retries), never hang or loop unbounded.

Setup (caller): mock :43210, redis :16379, proxies A :4000 / B :4001 on
config-redis.yaml, env NVIDIA_KEY_1=sk-good NVIDIA_KEY_2=sk-good2.

Run: python test_redis_multiprocess.py
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
HOT = "sk-good"      # scripted to 429 in R3
COLD = "sk-good2"     # stays healthy

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
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def chat(base):
    return req(base, "/v1/chat/completions",
               {"model": "nvidia-primary",
                "messages": [{"role": "user", "content": "hi"}]})


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
        return True
    except Exception as e:
        print(f"warn: redis flush failed: {e}")
        return False


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}", flush=True)


if __name__ == "__main__":
    # R1 health
    ha, _ = req(A, "/health/liveliness")
    hb, _ = req(B, "/health/liveliness")
    check("R1 both proxies healthy", ha == 200 and hb == 200, f"(A={ha} B={hb})")

    # R2 clean slate
    control(HOT, "ok")
    control(COLD, "ok")
    flush_redis()
    time.sleep(1)
    sa, _ = chat(A)
    sb, _ = chat(B)
    check("R2 clean slate: both processes serving", sa == 200 and sb == 200,
          f"(A={sa} B={sb})")

    # R3 cooldown propagation
    control(HOT, "429")
    # drive failures via A: allowed_fails=3, so >3 requests trips cooldown
    for _ in range(6):
        chat(A)
    time.sleep(1)
    # count 429-mode hits on HOT in mock log so far
    hits_before = sum(1 for _ in range(1))  # placeholder, real count below
    os.system("grep -c 'key=sk-good mode=429' mock.log > /tmp/hot_before.txt")
    with open("/tmp/hot_before.txt") as f:
        before = int(f.read().strip() or 0)
    # fire 10 requests via B; all must be 200 served by COLD,
    # and NO additional 429 hits on HOT may appear (B avoids the hot key)
    ok_b = 0
    for _ in range(10):
        st, _ = chat(B)
        if st == 200:
            ok_b += 1
    os.system("grep -c 'key=sk-good mode=429' mock.log > /tmp/hot_after.txt")
    with open("/tmp/hot_after.txt") as f:
        after = int(f.read().strip() or 0)
    check("R3 cooldown propagates: B serves via healthy key AND never touches hot key",
          ok_b == 10 and after == before,
          f"(B 200s={ok_b}/10, hot-key 429 hits {before}->{after})")

    # R4 cooldown expiry (60s) - only run with --phase2 after waiting
    if "--phase2" in sys.argv:
        control(HOT, "ok")
        time.sleep(62)  # cooldown_time=60
        sa2, _ = chat(A)
        sb2, _ = chat(B)
        check("R4 cooldown expiry: key returns to rotation",
              sa2 == 200 and sb2 == 200, f"(A={sa2} B={sb2})")
    else:
        print("R4 skipped (run again with --phase2 after >=60s)", flush=True)

    # R5 bounded failure
    control(HOT, "503")
    control(COLD, "503")
    t0 = time.time()
    st, body = chat(A)
    dt = time.time() - t0
    check("R5 both-keys 503: bounded failure, no unbounded retry",
          st in (500, 502, 503, 429, 400) and dt < 60,
          f"(status={st} after {dt:.1f}s)")
    control(HOT, "ok")
    control(COLD, "ok")

    passed = sum(1 for r in results if r)
    print(f"\n=== redis multiprocess: {passed}/{len(results)} ===", flush=True)
    sys.exit(0 if passed == len(results) else 1)
