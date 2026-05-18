#!/usr/bin/env python3
"""
comparison-service — core traffic comparison service.
Receives old-service responses from sidecar, compares against multiple
new service targets concurrently, computes structured diffs, and reports to Grafana.
"""
import os
import json
import base64
import time
import uuid
import logging
import threading
import argparse
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("comparison")

# ─── Severity Constants ────────────────────────────────────────────────────────

SEVERITY_NONE   = "NONE"
SEVERITY_LOW    = "LOW"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_HIGH   = "HIGH"

SEVERITY_ORDER = {SEVERITY_NONE: 0, SEVERITY_LOW: 1, SEVERITY_MEDIUM: 2, SEVERITY_HIGH: 3}

def merge_severities(severities):
    return max(severities, key=lambda s: SEVERITY_ORDER.get(s, 0), default=SEVERITY_NONE)

def judge_severity(status_diff, header_diffs, body_diffs, latency_diff_ms, new_err):
    if new_err:
        return SEVERITY_HIGH
    if status_diff:
        return SEVERITY_HIGH
    if len(body_diffs) > 5 or len(header_diffs) > 3:
        return SEVERITY_HIGH
    if latency_diff_ms > 5000:
        return SEVERITY_HIGH
    if body_diffs:
        return SEVERITY_MEDIUM
    if header_diffs:
        return SEVERITY_MEDIUM
    if latency_diff_ms > 1000:
        return SEVERITY_MEDIUM
    if latency_diff_ms > 200:
        return SEVERITY_LOW
    return SEVERITY_NONE

# ─── JSON Diff ────────────────────────────────────────────────────────────────

HEADER_KEYS = {
    "content-type", "content-length", "cache-control", "authorization",
    "x-request-id", "x-correlation-id", "x-service-version",
    "etag", "server", "date", "x-correlation-id",
}

def is_header(path):
    key = path.rsplit(".", 1)[-1].lower()
    return key in HEADER_KEYS or key.startswith("x-")

def should_ignore(path, ignore_fields):
    for ig in ignore_fields:
        ig = ig.replace("$.", "")
        if path == ig:
            return True
        if ig.endswith("*") and path.startswith(ig[:-1]):
            return True
    return False

@dataclass
class FieldDiff:
    path: str
    old_value: str
    new_value: str

def json_diff(old_raw: bytes, new_raw: bytes, ignore_fields: list) -> tuple:
    """Returns (header_diffs, body_diffs) as lists of FieldDiff."""
    try:
        old_data = json.loads(old_raw) if old_raw else {}
        new_data = json.loads(new_raw) if new_raw else {}
    except (json.JSONDecodeError, TypeError):
        if old_raw == new_raw:
            return [], []
        return [], [FieldDiff(path="root", old_value=str(old_raw), new_value=str(new_raw))]

    header_diffs, body_diffs = [], []

    def walk(path, o, n):
        if should_ignore(path, ignore_fields):
            return
        if isinstance(o, dict) and isinstance(n, dict):
            for k in set(list(o.keys()) + list(n.keys())):
                walk(f"{path}.{k}" if path else k, o.get(k), n.get(k))
        elif isinstance(o, list) and isinstance(n, list):
            for i in range(max(len(o), len(n))):
                walk(f"{path}[{i}]", o[i] if i < len(o) else None, n[i] if i < len(n) else None)
        else:
            if o != n:
                diff = FieldDiff(path=path or "root", old_value=str(o), new_value=str(n))
                if is_header(path):
                    header_diffs.append(diff)
                else:
                    body_diffs.append(diff)

    walk("", old_data, new_data)
    return header_diffs, body_diffs

# ─── Config ───────────────────────────────────────────────────────────────────

def load_yaml(path: str) -> dict:
    import yaml  # lazy import, yaml is installed
    with open(path) as f:
        return yaml.safe_load(f)

class Config:
    def __init__(self, path: str):
        self._cfg = load_yaml(path)
        log.info(f"Loaded config: {len(self._cfg.get('rules', []))} rules from {path}")

    def get_rule(self, name: str):
        for r in self._cfg.get("rules", []):
            if r.get("name") == name:
                return r
        return None

# ─── HTTP Client ──────────────────────────────────────────────────────────────

def http_request(method, url, headers=None, body=None, timeout=10):
    """Returns (status_code, headers_dict, body_bytes, latency_ms, error_str)."""
    start = time.time()
    try:
        h = dict(headers) if headers else {}
        req = Request(url, data=body, headers=h, method=method)
        with urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read()
            resp_headers = {k: v for k, v in resp.headers.items()}
            return resp.status, resp_headers, resp_body, int((time.time() - start) * 1000), None
    except URLError as e:
        return 0, {}, b"", int((time.time() - start) * 1000), str(e.reason)
    except Exception as e:
        return 0, {}, b"", int((time.time() - start) * 1000), str(e)

# ─── Comparison Engine ────────────────────────────────────────────────────────

class ComparisonEngine:
    def __init__(self, config: Config, loki_url: str = ""):
        self.config = config
        self.loki_url = loki_url

    def compare(self, payload: dict) -> dict:
        service_pair = payload.get("service_pair", "unknown")
        rule = self.config.get_rule(service_pair)
        if not rule:
            raise ValueError(f"No rule for service_pair={service_pair!r}")

        endpoint = payload.get("endpoint", "/")
        method = payload.get("method", "GET")
        shadow = payload.get("shadow", {})
        ignore_fields = self._get_ignore_fields(rule, endpoint, method)

        old_status   = shadow.get("status_code", 0)
        old_latency  = shadow.get("latency_ms", 0)
        old_body_b64 = shadow.get("body_b64", "")
        old_headers  = shadow.get("headers", {})

        try:
            old_body = base64.b64decode(old_body_b64)
        except Exception:
            old_body = old_body_b64.encode()

        # Concurrently fetch all targets
        targets = rule.get("targets", [])
        results = []
        lock = threading.Lock()

        def fetch_target(tgt):
            result = self._compare_target(
                tgt, method, endpoint,
                old_body, old_headers,
                old_status, old_latency,
                ignore_fields,
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=fetch_target, args=(tgt,)) for tgt in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        severities = [r.get("severity", SEVERITY_NONE) for r in results]

        return {
            "report_id":    str(uuid.uuid4()),
            "service_pair": service_pair,
            "endpoint":     endpoint,
            "method":       method,
            "severity":     merge_severities(severities),
            "shadow": {
                "status_code": old_status,
                "latency_ms":  old_latency,
                "body_len":    len(old_body),
            },
            "targets": results,
            "compared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def _compare_target(self, tgt, method, endpoint,
                        old_body, old_headers,
                        old_status, old_latency,
                        ignore_fields):
        url = tgt["url"] + endpoint
        # Forward relevant headers (skip hop-by-hop)
        h = {}
        skip = {"host", "content-length", "transfer-encoding", "connection"}
        for k, v in old_headers.items():
            if k.lower() not in skip:
                h[k] = v

        status, resp_headers, resp_body, latency, err = http_request(
            method, url, h,
            old_body if method != "GET" else None,
        )

        if err:
            return {
                "target_name":     tgt["name"],
                "target_version":  tgt.get("version", ""),
                "url":             url,
                "status_code":     0,
                "latency_ms":      latency,
                "status_diff":     True,
                "header_diff":     [],
                "body_diff":       [],
                "latency_diff_ms": latency - old_latency,
                "severity":        SEVERITY_HIGH,
                "error":           err,
            }

        status_diff = status != old_status
        header_diffs, body_diffs = json_diff(old_body, resp_body, ignore_fields)
        severity = judge_severity(status_diff, header_diffs, body_diffs,
                                   latency - old_latency, "")

        return {
            "target_name":     tgt["name"],
            "target_version":  tgt.get("version", ""),
            "url":             url,
            "status_code":     status,
            "latency_ms":      latency,
            "status_diff":     status_diff,
            "header_diff":     [{"path": d.path, "old_value": d.old_value, "new_value": d.new_value} for d in header_diffs],
            "body_diff":       [{"path": d.path, "old_value": d.old_value, "new_value": d.new_value} for d in body_diffs],
            "latency_diff_ms": latency - old_latency,
            "severity":        severity,
            "error":           "",
        }

    def _get_ignore_fields(self, rule, endpoint, method):
        for ep in rule.get("endpoints", []):
            if ep.get("path") == endpoint and ep.get("method") == method:
                return ep.get("ignore_fields", [])
        return []

# ─── Grafana Reporter ─────────────────────────────────────────────────────────

def report_to_grafana(loki_url: str, data: dict):
    """Fire-and-forget Loki push."""
    if not loki_url:
        n = len(data.get("targets", []))
        sevs = [t["severity"] for t in data.get("targets", [])]
        log.info(f"[grafana] (loki disabled) severity={data['severity']} "
                 f"targets=[{', '.join(sevs)}] endpoint={data['endpoint']}")
        return
    try:
        payload = json.dumps({
            "streams": [{
                "stream": {
                    "service_pair": data["service_pair"],
                    "severity":     data["severity"],
                    "type":         "summary",
                },
                "values": [[str(int(time.time() * 1e9)), json.dumps(data)]]
            }]
        }).encode()
        req = Request(loki_url + "/loki/api/v1/push", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as r:
            log.info(f"[grafana] loki pushed OK status={r.status}")
    except Exception as e:
        log.warning(f"[grafana] loki push failed: {e}")

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    engine: ComparisonEngine = None
    loki_url: str = ""

    def do_POST(self):
        if self.path != "/compare":
            self.respond_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.respond_json(400, {"error": "invalid JSON"})
            return

        try:
            result = self.engine.compare(payload)
        except ValueError as e:
            self.respond_json(404, {"error": str(e)})
            return
        except Exception as e:
            log.exception("compare error")
            self.respond_json(500, {"error": str(e)})
            return

        # Async Grafana report (non-blocking)
        loki = self.loki_url
        threading.Thread(target=report_to_grafana, args=(loki, result), daemon=True).start()
        self.respond_json(200, result)

    def do_GET(self):
        if self.path == "/health":
            self.respond_json(200, {"status": "ok"})
        elif self.path == "/rules":
            self.respond_json(200, self.engine.config._cfg)
        elif self.path == "/":
            self.respond_json(200, {"service": "comparison-service", "version": "1.0.0"})
        else:
            self.respond_json(404, {"error": "not found"})

    def respond_json(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info(f"{self.address_string()} {fmt % args}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Traffic Comparison Service")
    parser.add_argument("--port",   type=int, default=8070)
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "config.yaml"))
    parser.add_argument("--loki",   default="")
    args = parser.parse_args()

    cfg = Config(args.config)
    Handler.engine  = ComparisonEngine(cfg, loki_url=args.loki)
    Handler.loki_url = args.loki

    srv = HTTPServer(("0.0.0.0", args.port), Handler, bind_and_activate=False)
    srv.allow_reuse_address = True  # survive SIGKILL of previous process on same port
    # Retry bind in case previous process is in TIME_WAIT
    for attempt in range(5):
        try:
            srv.server_bind()
            break
        except OSError as e:
            if attempt < 4:
                import time as _time
                log.warning(f"Port {args.port} not ready (attempt {attempt+1}/5): {e} — retrying in 2s...")
                _time.sleep(2)
            else:
                raise
    srv.server_activate()
    log.info(f"comparison-service listening on :{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down...")
        srv.shutdown()

if __name__ == "__main__":
    main()
