#!/usr/bin/env python3
"""
traffic-mirror-sidecar — intercepts mirrored traffic and reports to comparison-service.

In the real K8s version, this would use iptables traffic redirection.
In the demo, we use a simple HTTP endpoint: POST /mirror

Flow:
  1. Test/client sends POST /mirror with original request details
  2. Sidecar forwards to old-service, captures response
  3. Returns old-service response to client (zero latency impact)
  4. Async: reports ShadowPayload to comparison-service /compare
"""
import json
import base64
import time
import uuid
import threading
import logging
import argparse
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sidecar")

# ─── Async Reporter ────────────────────────────────────────────────────────────

def report_async(comparison_url: str, payload: dict, timeout_ms: int = 3000):
    def _run():
        try:
            body = json.dumps(payload).encode()
            req = Request(
                comparison_url + "/compare",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=timeout_ms / 1000) as resp:
                result = json.loads(resp.read())
                sev = result.get("severity", "?")
                rid = result.get("report_id", "?")
                log.info(f"✅ reported → report_id={rid[:8]}... severity={sev} "
                         f"endpoint={payload.get('endpoint')}")
        except Exception as e:
            log.warning(f"⚠️  report failed: {e} (endpoint={payload.get('endpoint')})")
    threading.Thread(target=_run, daemon=True).start()

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    comparison_url: str = ""
    old_service_url: str = ""
    fire_and_forget: bool = True

    def do_POST(self):
        if self.path != "/mirror":
            self.respond_json(404, {"error": "only /mirror is supported"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""

        service_pair = self.headers.get("X-Service-Pair", "default-rule")
        endpoint = self.headers.get("X-Original-Endpoint", "/")
        method = self.headers.get("X-Original-Method", "GET")

        start = time.time()

        # Forward to old service
        target_url = self.old_service_url + endpoint
        h = {}
        skip = {"host", "content-length", "transfer-encoding", "connection",
                "x-service-pair", "x-original-endpoint", "x-original-method"}
        for k, v in self.headers.items():
            if k.lower() not in skip:
                h[k] = v

        try:
            req = Request(target_url, data=body if method != "GET" else None,
                          headers=h, method=method)
            with urlopen(req, timeout=10) as upstream:
                resp_body = upstream.read()
                resp_status = upstream.status
                resp_headers = {k: v for k, v in upstream.headers.items()}
        except URLError as e:
            resp_status = 502
            resp_body = json.dumps({"error": f"upstream error: {e.reason}"}).encode()
            resp_headers = {"content-type": "application/json"}
        except Exception as e:
            resp_status = 502
            resp_body = json.dumps({"error": str(e)}).encode()
            resp_headers = {"content-type": "application/json"}

        latency_ms = int((time.time() - start) * 1000)

        # Return old-service response to caller (zero modification)
        self.send_response(resp_status)
        for k, v in resp_headers.items():
            if k.lower() not in {"transfer-encoding", "connection"}:
                self.send_header(k, v)
        self.send_header("Content-Length", len(resp_body))
        self.end_headers()
        self.wfile.write(resp_body)

        # Build ShadowPayload and report to comparison-service
        shadow_headers = {k: v for k, v in resp_headers.items()
                          if k.lower() not in {"transfer-encoding", "connection", "content-length"}}

        payload = {
            "id": str(uuid.uuid4()),
            "service_pair": service_pair,
            "endpoint": endpoint,
            "method": method,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "shadow": {
                "status_code": resp_status,
                "headers": shadow_headers,
                "body_b64": base64.b64encode(resp_body).decode(),
                "latency_ms": latency_ms,
            },
        }

        if self.fire_and_forget:
            report_async(self.comparison_url, payload)
        else:
            report_async(self.comparison_url, payload)

    def do_GET(self):
        if self.path == "/health":
            self.respond_json(200, {"status": "ok", "role": "traffic-mirror-sidecar"})
        else:
            self.respond_json(200, {"service": "sidecar", "version": "1.0.0"})

    def respond_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet default logging

def main():
    parser = argparse.ArgumentParser(description="Traffic Mirror Sidecar")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--comparison-url", default=os.environ.get("COMPARISON_URL", "http://localhost:8080"))
    parser.add_argument("--old-service-url", default=os.environ.get("OLD_SERVICE_URL", "http://localhost:9001"))
    args = parser.parse_args()

    Handler.comparison_url = args.comparison_url
    Handler.old_service_url = args.old_service_url
    Handler.fire_and_forget = True

    srv = HTTPServer(("0.0.0.0", args.port), Handler)
    srv.allow_reuse_address = True
    log.info(f"[sidecar] listening on :{args.port}")
    log.info(f"[sidecar] comparison_url={args.comparison_url}  old_service={args.old_service_url}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down...")
        srv.shutdown()

if __name__ == "__main__":
    main()
