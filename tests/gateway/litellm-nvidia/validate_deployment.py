#!/usr/bin/env python3
"""Fail-closed deployment validation for the LiteLLM NVIDIA gateway.

Exits 0 ONLY if every gate passes:
  1. installed LiteLLM version == pinned version (LITELLM_PIN)
  2. streaming-integrity patch is applied to the installed package
  3. gateway health endpoint responds
  4. 5-case streaming differential suite passes (A-E), including
     case C (the regression canary) and case E (valid tool-call streams)
  5. 11-scenario acceptance suite passes

Any failure exits non-zero. Deployment automation MUST treat a non-zero
exit as "do not route production traffic through this gateway".

Usage:
    ./validate_deployment.py            # full validation
    ./validate_deployment.py --check-patch-only

Environment:
    LITELLM_PIN         required pin, default 1.104.2
    GATEWAY_MASTER_KEY  gateway master key (test key in local runs)
    Assumes mock_nvidia.py on :43210 and the proxy on :4000 are running.
"""

import importlib.metadata
import os
import re
import subprocess
import sys
import urllib.request

PIN = os.environ.get("LITELLM_PIN", "1.104.2")
HERE = os.path.dirname(os.path.abspath(__file__))
GATE = 0


def fail(msg):
    global GATE
    GATE = 1
    print(f"FAIL  {msg}", flush=True)


def ok(msg):
    print(f"PASS  {msg}", flush=True)


def gate_version():
    try:
        v = importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        fail("litellm not installed")
        return
    if v == PIN:
        ok(f"litellm=={v} matches pin {PIN}")
    else:
        fail(f"litellm=={v} != pinned {PIN} - upgrade or change LITELLM_PIN "
             f"and regenerate the patch")


def gate_patch():
    try:
        import litellm
    except ImportError as e:
        fail(f"litellm not importable: {e}")
        return
    pkg_dir = getattr(litellm, "__file__", None)
    if not pkg_dir:
        fail("cannot locate litellm package directory")
        return
    target = os.path.join(os.path.dirname(os.path.abspath(pkg_dir)),
                          "litellm_core_utils", "streaming_handler.py")
    try:
        src = open(target).read()
    except OSError as e:
        fail(f"cannot read {target}: {e}")
        return
    if "refusing to synthesize a successful completion" in src:
        ok(f"clean-EOF patch present in {target}")
    else:
        fail("clean-EOF patch MISSING - incomplete streams would be "
             "reported as successful completions. Apply "
             "streaming_handler_clean-EOF.patch and re-run.")


def gate_health():
    try:
        code = urllib.request.urlopen(
            "http://localhost:4000/health/liveliness", timeout=10).status
        if code == 200:
            ok("gateway health 200")
        else:
            fail(f"gateway health returned {code}")
    except Exception as e:
        fail(f"gateway unreachable: {e}")


def gate_suites():
    global GATE
    for script, label in (("test_streaming_integrity.py", "streaming differential"),
                          ("run_acceptance.py", "acceptance")):
        r = subprocess.run([sys.executable, os.path.join(HERE, script)],
                           capture_output=True, text=True, timeout=600)
        tail = (r.stdout or "").strip().splitlines()[-1:] or ["<no output>"]
        if r.returncode == 0:
            ok(f"{label}: {tail[0]}")
        else:
            fail(f"{label} FAILED ({tail[0]}) - full output follows")
            print(r.stdout[-3000:], flush=True)
            print(r.stderr[-1000:], flush=True)


if __name__ == "__main__":
    check_only = "--check-patch-only" in sys.argv
    gate_version()
    gate_patch()
    if not check_only:
        gate_health()
        gate_suites()
    print("\nDEPLOYMENT VALIDATION: " + ("PASSED" if GATE == 0 else "FAILED - DO NOT ROUTE PRODUCTION TRAFFIC"), flush=True)
    sys.exit(GATE)
