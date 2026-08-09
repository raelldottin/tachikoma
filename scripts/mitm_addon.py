"""
mitmproxy addon: capture ALL Pixel Starships and Unity auth traffic.
"""
import json
import os
from datetime import datetime
from mitmproxy import http

LOG_FILE = os.path.expanduser("~/pss-mitm-capture.jsonl")

INTERESTING_HOSTS = [
    "player-auth.services.api.unity.com",
    "api.pixelstarships.com",
    "unity.com",
    "unity3d.com",
    "pixelstarships",
]

def is_interesting(host):
    return any(h in host for h in INTERESTING_HOSTS)

def request(flow: http.HTTPFlow):
    host = flow.request.pretty_host
    if is_interesting(host):
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().isoformat(),
                "type": "request",
                "url": flow.request.pretty_url,
                "method": flow.request.method,
                "headers": dict(flow.request.headers),
                "body": flow.request.get_text() or "",
            }) + "\n")

def response(flow: http.HTTPFlow):
    host = flow.request.pretty_host
    if is_interesting(host):
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().isoformat(),
                "type": "response",
                "url": flow.request.pretty_url,
                "status_code": flow.response.status_code,
                "headers": dict(flow.response.headers),
                "body": flow.response.get_text() or "",
            }) + "\n")
