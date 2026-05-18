#!/usr/bin/env python3
"""
new-service-v1 — new service (v2.0.0).
Differences from old-service:
  - data[0].name: "Alice" → "Alicia"
  - data[0].score: 95.5 (float) → 96 (int)
  - "verified" field removed
  - meta.plan_tier: "enterprise" → "business"
  - meta.tier_code: new field added
  - "server": "new-v1" added to response
  - cache-control: "no-store" (different from old)
"""
import json
import random
import time
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler

VERSION = "v2.0.0"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.respond_json(200, {"status": "ok", "version": VERSION})
        elif self.path.startswith("/api/v1/users"):
            self.handle_users()
        elif self.path.startswith("/api/v1/products"):
            self.handle_products()
        else:
            self.respond_json(200, {"service": "new-service-v1", "version": VERSION})

    def handle_users(self):
        time.sleep(random.uniform(0.040, 0.070))  # slightly slower
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Service-Version", VERSION)
        self.send_header("Cache-Control", "no-store")  # ← different from old
        body = json.dumps({
            "data": [
                {
                    "id": 123,
                    "name": "Alicia",           # ← changed: Alice → Alicia
                    "email": "alice@example.com",
                    "status": "active",
                    "created_at": "2024-01-01T00:00:00Z",
                    "score": 96,                # ← changed: float → int
                    # "verified" field removed
                    "tags": ["premium", "early-adopter"],
                    "meta": {
                        "source": "old-db",
                        "region": "us-east-1",
                        "plan_tier": "business",  # ← changed: enterprise → business
                        "tier_code": 2,            # ← new field
                    },
                },
                {
                    "id": 456,
                    "name": "Bob",
                    "email": "bob@example.com",
                    "status": "inactive",
                    "created_at": "2024-02-15T10:30:00Z",
                    "score": 72,                 # ← int
                    # "verified" removed
                    "tags": ["trial"],
                    "meta": {
                        "source": "new-db",
                        "region": "us-west-2",
                        "plan_tier": "free",
                        "tier_code": 0,
                    },
                },
            ],
            "total": 2,
            "page": 1,
            "request_id": f"req-{random.randint(100000, 999999)}",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "server": "new-v1",                 # ← new field
        }).encode()
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_products(self):
        time.sleep(random.uniform(0.015, 0.025))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Service-Version", VERSION)
        body = json.dumps({
            "data": [
                {
                    "id": 1,
                    "name": "Widget Pro",
                    "price": 29.99,
                    "currency": "USD",
                    "in_stock": True,
                    "stock_count": 150,
                    "category": "electronics",  # ← new field
                },
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).encode()
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[new-service-v1] {fmt % args}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9002)
    args = parser.parse_args()
    srv = HTTPServer(("0.0.0.0", args.port), Handler)
    srv.allow_reuse_address = True
    print(f"[new-service-v1] listening on :{args.port} ({VERSION})")
    srv.serve_forever()

if __name__ == "__main__":
    main()
