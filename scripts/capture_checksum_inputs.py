#!/usr/bin/env python3
"""
mitmproxy addon to capture Pixel Starships checksum inputs.
Captures: GetLatestVersion4, DeviceLogin17, CollectMarker2, RebuildAmmo3, UserEmailPasswordAuthorize4
"""

import json
import mitmproxy.http
from mitmproxy import ctx

TARGET_HOST = "api.pixelstarships.com"
TARGET_PATHS = [
    "/SettingService/GetLatestVersion3",
    "/SettingService/GetLatestVersion4",
    "/UserService/DeviceLogin17",
    "/GalaxyService/CollectMarker2",
    "/RoomService/RebuildAmmo3",
    "/UserService/UserEmailPasswordAuthorize4",
    "/ShopService/PurchaseCatalog2",
    "/LibeOpsService/GetCatalogQuantity",
]

class CaptureChecksumInputs:
    def __init__(self):
        self.captured = []
    
    def request(self, flow: mitmproxy.http.HTTPFlow):
        if flow.request.host != TARGET_HOST:
            return
        
        path = flow.request.path
        if not any(p in path for p in TARGET_PATHS):
            return
        
        entry = {
            "timestamp": flow.request.timestamp_start,
            "method": flow.request.method,
            "url": flow.request.url,
            "path": path,
            "headers": dict(flow.request.headers),
            "query": dict(flow.request.query),
        }
        
        if flow.request.content:
            try:
                if flow.request.headers.get("content-type", "").startswith("application/json"):
                    entry["json"] = json.loads(flow.request.content)
                else:
                    entry["form"] = dict(flow.request.urlencoded_form)
            except Exception:
                entry["body_raw"] = flow.request.content.decode('utf-8', errors='ignore')[:500]
        
        self.captured.append(("request", entry))
        ctx.log.info(f"Captured request: {path}")
    
    def response(self, flow: mitmproxy.http.HTTPFlow):
        if flow.request.host != TARGET_HOST:
            return
        
        path = flow.request.path
        if not any(p in path for p in TARGET_PATHS):
            return
        
        entry = {
            "timestamp": flow.response.timestamp_start,
            "status_code": flow.response.status_code,
            "headers": dict(flow.response.headers),
        }
        
        if flow.response.content:
            try:
                entry["text"] = flow.response.content.decode('utf-8', errors='ignore')
            except Exception:
                entry["body_raw"] = str(flow.response.content)[:500]
        
        self.captured.append(("response", entry))
        ctx.log.info(f"Captured response: {path} ({flow.response.status_code})")
    
    def done(self):
        output_file = ctx.options.capture_file if hasattr(ctx.options, 'capture_file') else "/tmp/pss_capture.json"
        with open(output_file, "w") as f:
            json.dump(self.captured, f, indent=2)
        ctx.log.info(f"Saved {len(self.captured)} entries to {output_file}")

addons = [CaptureChecksumInputs()]