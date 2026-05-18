#!/usr/bin/env python3
"""
new-service-v2 — new service (v3.1.0).
Severe differences from old-service (should trigger HIGH severity):
  - /api/v1/users/456: returns HTTP 500 (not 200)
  - Latency: >2100ms (extremely slow)
  - data[0].in_stock: true → false (product endpoint)
"""
import json
import random
import time
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler

VERSION = "v3.1.0"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.respond_json(200, {"status": "ok", "version": VERSION})
        elif self.path.startswith("/api/v1/users"):
            self.handle_users()
        elif self.path.startswith("/api/v1/products"):
            self.handle_products()
        else:
            self.respond_json(200, {"service": "new-service-v2", "version": VERSION})

    def handle_users(self):
        # User 456 gets a 500 error — simulates error on some users
        if "/456" in self.path:
            time.sleep(random.uniform(2.1, 2.3))  # slow
            body = json.dumps({
                "error": "internal_server_error",
                "message": "database connection failed",
                "code": 500,
            }).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Service-Version", VERSION)
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # User 123 is slower but OK
        time.sleep(random.uniform(0.060, 0.090))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Service-Version", VERSION)
        self.send_header("Cache-Control", "no-store")
        body = json.dumps({
            "data": [
                {
                    "id": 123,
                    "name": "Alice",           # same as old
                    "email": "alice@example.com",
                    "status": "active",
                    "created_at": "2024-01-01T00:00:00Z",
                    "score": 95.5,
                    "verified": True,
                    "tags": ["premium", "early-adopter"],
                    "meta": {
                        "source": "old-db",
                        "region": "us-east-1",
                        "plan_tier": "enterprise",
                        "tier_code": 3,        # new field
                    },
                },
            ],
            "total": 1,
            "page": 1,
            "request_id": f"req-{random.randint(100000, 999999)}",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "server": "new-v2",
        }).encode()
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_products(self):
        time.sleep(random.uniform(0.025, 0.045))
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
                    "in_stock": False,       # ← different: true → false
                    "stock_count": 0,        # ← different: 150 → 0
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
        print(f"[new-service-v2] {fmt % args}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9003)
    args = parser.parse_args()
    srv = HTTPServer(("0.0.0.0", args.port), Handler)
    srv.allow_reuse_address = True
    print(f"[new-service-v2] listening on :{args.port} ({VERSION})")
    srv.serve_forever()

if __name__ == "__main__":
    main()
