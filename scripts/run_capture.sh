#!/bin/bash
# Pixel Starships mitmproxy capture runner
# Captures GetLatestVersion4, DeviceLogin17, CollectMarker2, RebuildAmmo3, UserEmailPasswordAuthorize4

set -euo pipefail

CAPTURE_DIR="/tmp/pss_capture"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
CAPTURE_FILE="${CAPTURE_DIR}/pss_capture_${TIMESTAMP}.json"
MITMPROXY_PORT=8080

mkdir -p "${CAPTURE_DIR}"

echo "Starting mitmproxy on port ${MITMPROXY_PORT}"
echo "Capture file: ${CAPTURE_FILE}"
echo ""
echo "=== SETUP INSTRUCTIONS ==="
echo "1. On your iOS device / macOS running Pixel Starships:"
echo "   Settings -> Wi-Fi -> [Your Network] -> Configure Proxy -> Manual"
echo "   Server: $(ipconfig getifaddr en0 2>/dev/null || hostname -I | awk '{print $1}')"
echo "   Port: ${MITMPROXY_PORT}"
echo ""
echo "2. Install mitmproxy CA certificate:"
echo "   Open http://mitm.it in Safari on the device"
echo "   Download and install the certificate for iOS/Android"
echo "   Settings -> General -> About -> Certificate Trust Settings -> Enable mitmproxy"
echo ""
echo "3. Launch Pixel Starships and perform actions to trigger endpoints:"
echo "   - Login (DeviceLogin17)"
echo "   - Enter galaxy (GetLatestVersion4)"
echo "   - Collect markers (CollectMarker2)"
echo "   - Rebuild ammo (RebuildAmmo3)"
echo "   - Email/password login if testing (UserEmailPasswordAuthorize4)"
echo "   - Purchase Scorched Pod (PurchaseCatalog2)"
echo ""
echo "4. Press Ctrl+C here to stop capture and save"
echo ""

# mitmproxy addon script
ADDON_SCRIPT=$(cat << 'EOF'
import json
import mitmproxy.http
from mitmproxy import ctx

TARGET_HOST = "api.pixelstarships.com"
TARGET_PATHS = [
    "/SettingService/GetLatestVersion4",
    "/UserService/DeviceLogin17",
    "/GalaxyService/CollectMarker2",
    "/RoomService/RebuildAmmo3",
    "/UserService/UserEmailPasswordAuthorize4",
    "/ShopService/PurchaseCatalog2",
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
        
        # Capture request details
        entry = {
            "timestamp": flow.request.timestamp_start,
            "method": flow.request.method,
            "url": flow.request.url,
            "path": path,
            "headers": dict(flow.request.headers),
            "query": dict(flow.request.query),
        }
        
        # Parse form/JSON body
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
        output_file = ctx.options.get("capture_file", "/tmp/pss_capture.json")
        with open(output_file, "w") as f:
            json.dump(self.captured, f, indent=2)
        ctx.log.info(f"Saved {len(self.captured)} entries to {output_file}")

addons = [CaptureChecksumInputs()]
EOF
)

echo "${ADDON_SCRIPT}" > /tmp/capture_addon.py

# Run mitmproxy with the addon
mitmproxy -p ${MITMPROXY_PORT} -s /tmp/capture_addon.py --set capture_file="${CAPTURE_FILE}" --ssl-insecure --no-http2