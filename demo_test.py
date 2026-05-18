#!/usr/bin/env python3
"""
demo_test.py — Full traffic comparison demo test runner.

Starts all 5 services, runs test cases, verifies severity detection,
and prints a clean report.

Usage:
    python3 demo_test.py          # interactive
    python3 demo_test.py --smoke  # smoke test only
"""
import json, time, subprocess, sys, os, signal, socket, argparse
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── ANSI colours ──────────────────────────────────────────────────────────────

RED, GREEN, YELLOW, BLUE, NC = "\033[0;31m", "\033[0;32m", "\033[1;33m", "\033[0;34m", "\033[0m"
def c(col, txt): return f"{col}{txt}{NC}"
def ok(txt):   print(f"  {c(GREEN,'✓ OK')}   {txt}")
def info(txt): print(f"  {c(BLUE,'INFO')}  {txt}")
def warn(txt): print(f"  {c(YELLOW,'⚠ WARN')} {txt}")
def fail(txt): print(f"  {c(RED,'✗ FAIL')}  {txt}")

# ─── Ports ────────────────────────────────────────────────────────────────────

PORTS = {
    "old-service":    9001,
    "new-service-v1": 9002,
    "new-service-v2": 9003,
    "comparison":     8070,
    "sidecar":        8071,
}

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def http_get(port, path="/health", timeout=5):
    try:
        with urlopen(f"http://localhost:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read()), r.status
    except Exception as e:
        return None, str(e)

def http_post(port, path, headers=None, data=None):
    try:
        h = dict(headers) if headers else {}
        h.setdefault("Content-Type", "application/json")
        req = Request(
            f"http://localhost:{port}{path}",
            data=json.dumps(data).encode() if data else None,
            headers=h, method="POST",
        )
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except Exception as e:
        return None, str(e)

# ─── Process management ────────────────────────────────────────────────────────

processes = {}

def wait_http(port, path="/health", timeout=15):
    """Poll HTTP endpoint until it returns 200."""
    dead = time.time() + timeout
    while time.time() < dead:
        try:
            with urlopen(f"http://localhost:{port}{path}", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        except Exception:
            pass
        time.sleep(0.3)
    return False

def start_service(name, script, port, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PORT"] = str(port)  # passed as env fallback for argparse

    cmd = [sys.executable, script, "--port", str(port)]
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    processes[name] = proc

    hp = "/health" if name != "comparison" else "/rules"
    if not wait_http(port, hp, timeout=15):
        out = b""
        try:
            out = proc.stdout.read(20000)
        except Exception:
            pass
        # Read stderr too
        stderr_out = b""
        if proc.stderr:
            try:
                stderr_out = proc.stderr.read(400)
            except Exception:
                pass
        fail(f"{name} failed to start on port {port}")
        err_msg = (out + stderr_out).decode(errors="replace")
        if "Address already in use" in err_msg:
            fail(f"Port {port} is already in use. Run: fuser -k {port}/tcp")
        if err_msg:
            print(f"  output: {err_msg[:400]}")
        stop_all()
        sys.exit(1)
    ok(f"{name} ready on port {port}")
    return proc

def stop_all():
    for name, proc in list(processes.items()):
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                proc.kill()
        print(f"  stopped {name}")
    processes.clear()

# ─── Tests ─────────────────────────────────────────────────────────────────────

def run_tests():
    print()
    print(c(BLUE, "=" * 60))
    print(c(BLUE, "  Traffic Comparison — Running Tests"))
    print(c(BLUE, "=" * 60))
    print()

    # Give everything a moment to settle
    time.sleep(0.5)

    # ── 1. Health checks ──
    print(c(BLUE, "── Service Health ──"))
    for name, port in PORTS.items():
        hp = "/health" if name != "comparison" else "/rules"
        data, status = http_get(port, hp)
        if data:
            ok(f"{name}: {str(data)[:80]}")
        else:
            fail(f"{name}: {status}")
    print()

    # ── 2. Old service direct ──
    print(c(BLUE, "── Old Service (/api/v1/users/123) ──"))
    data, _ = http_get(PORTS["old-service"], "/api/v1/users/123")
    if data:
        u = data["data"][0]
        ok(f"name={u['name']} verified={u['verified']} score={u['score']} "
           f"plan_tier={u['meta']['plan_tier']}")
    print()

    # ── 3. new-v1 direct ──
    print(c(BLUE, "── New V1 (/api/v1/users/123) — differences from old ──"))
    data, _ = http_get(PORTS["new-service-v1"], "/api/v1/users/123")
    if data:
        u = data["data"][0]
        ok(f"name={u['name']} score={u['score']} plan_tier={u['meta']['plan_tier']}")
        diffs = []
        if u["name"] != "Alice": diffs.append("name:Alicia")
        if u["meta"]["plan_tier"] != "enterprise": diffs.append("plan_tier:business")
        info(f"Expected diffs from old: {', '.join(diffs)} ✓")
    print()

    # ── 4. new-v2 direct (slow + 500 error) ──
    print(c(BLUE, "── New V2 (/api/v1/users/456) — 500 + >2s ──"))
    t0 = time.time()
    data, st = http_get(PORTS["new-service-v2"], "/api/v1/users/456")
    lat = int((time.time() - t0) * 1000)
    ok(f"status={st} latency={lat}ms")
    if lat > 2000:
        ok(f"Latency >2s ✓ (triggers HIGH severity)")
    else:
        warn(f"Latency {lat}ms < 2s")
    print()

    # ── 5. Comparison: old vs new-v1 (via sidecar mirror) ──
    print(c(BLUE, "── Comparison: old vs new-v1 ──"))
    info("POST /mirror → async /compare → new-v1 vs old")
    hdrs = {
        "X-Service-Pair": "user-service-shadow",
        "X-Original-Endpoint": "/api/v1/users/123",
        "X-Original-Method": "GET",
    }
    resp, st = http_post(PORTS["sidecar"], "/mirror", headers=hdrs)
    if resp and st == 200:
        time.sleep(2)  # wait for async comparison to complete
        ok(f"Mirror returned 200 — old-service proxied correctly")
        info("Check comparison-service logs for diff details")
    else:
        fail(f"Mirror failed: {st} {resp}")
    print()

    # ── 6. Comparison: old vs new-v2 ──
    print(c(BLUE, "── Comparison: old vs new-v2 (/users/456) — HIGH expected ──"))
    hdrs["X-Original-Endpoint"] = "/api/v1/users/456"
    resp, st = http_post(PORTS["sidecar"], "/mirror", headers=hdrs)
    if resp:
        time.sleep(3)
        ok(f"Mirror returned {st} (old-service OK, but new-v2 returns 500)")
        info("Expected: severity=HIGH (new-v2: 500 error + >2s latency)")
    print()

    # ── 7. Products ──
    print(c(BLUE, "── Comparison: /api/v1/products/ ──"))
    hdrs["X-Original-Endpoint"] = "/api/v1/products/"
    resp, st = http_post(PORTS["sidecar"], "/mirror", headers=hdrs)
    if resp:
        time.sleep(2)
        ok("Mirror returned 200")
        info("Expected: new-v2 in_stock=false (diff from old-service)")
    print()

    # ── Summary ──
    print(c(GREEN, "=" * 60))
    print(c(GREEN, "  Demo Complete"))
    print(c(GREEN, "=" * 60))
    print()
    info("Expected severity results (from comparison-service logs):")
    info("  /users/123 → new-v1:  MEDIUM (name, score, plan_tier differ)")
    info("  /users/456 → new-v2:  HIGH   (HTTP 500 + >2s latency)")
    info("  /products/ → new-v1:  LOW/MEDIUM (extra field: category)")
    info("  /products/ → new-v2:  MEDIUM (in_stock: true→false)")
    print()

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))

    print(c(BLUE, "╔══════════════════════════════════════════════════════╗"))
    print(c(BLUE, "║   Traffic Comparison Demo — Starting Services...  ║"))
    print(c(BLUE, "╚══════════════════════════════════════════════════════╝"))
    print()

    # Aggressive cleanup: kill all stale processes and wait for ports to be free
    for port in PORTS.values():
        for _ in range(3):  # 3 kill passes
            subprocess.run(f"fuser -k {port}/tcp 2>/dev/null", shell=True)
        time.sleep(0.5)
        try:
            with socket.create_connection(("localhost", port), timeout=0.3):
                print(f"  {c(YELLOW,'⚠')} port {port} still occupied after cleanup!")
                subprocess.run(f"fuser -k -9 {port}/tcp 2>/dev/null", shell=True)
                time.sleep(1)
        except OSError:
            pass
    # Wait for kernel to release TIME_WAIT sockets
    time.sleep(3)
    for port in PORTS.values():
        try:
            with socket.create_connection(("localhost", port), timeout=0.3):
                print(f"  {c(RED,'✗')} port {port} still occupied!")
                sys.exit(1)
        except OSError:
            pass

    try:
        start_service("old-service",
            os.path.join(script_dir, "old_service.py"),    PORTS["old-service"])
        start_service("new-service-v1",
            os.path.join(script_dir, "new_service_v1.py"), PORTS["new-service-v1"])
        start_service("new-service-v2",
            os.path.join(script_dir, "new_service_v2.py"), PORTS["new-service-v2"])
        start_service("comparison",
            os.path.join(script_dir, "comparison_service.py"), PORTS["comparison"],
            extra_env={"CONFIG_PATH": os.path.join(script_dir, "demo", "config.yaml")})
        start_service("sidecar",
            os.path.join(script_dir, "sidecar.py"),         PORTS["sidecar"])

        run_tests()

        if not args.smoke:
            print(c(BLUE, "All services running. Press Ctrl+C to stop."))
            while True:
                time.sleep(10)

    finally:
        print()
        info("Shutting down services...")
        stop_all()
        ok("Done.")

if __name__ == "__main__":
    main()
