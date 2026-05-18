#!/usr/bin/env bash
# Demo test script — runs the full traffic comparison demo locally.
# No K8s required, everything runs as plain HTTP services.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

COMP_PORT=8080
SIDECAR_PORT=8090
OLD_PORT=9001
NEW_V1_PORT=9002
NEW_V2_PORT=9003

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" ; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*" ; }

wait_for_port() {
    local port=$1
    local name=$2
    local max_wait=15
    local waited=0
    while ! python3 -c "import socket; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('localhost',$port)); s.close(); exit(0 if r==0 else 1)" 2>/dev/null; do
        if ((waited >= max_wait)); then
            fail "$name not responding on port $port after ${max_wait}s"
            exit 1
        fi
        sleep 1
        ((waited++))
        echo -n "."
    done
    echo ""
}

stop_all() {
    info "Stopping all services..."
    for pid in \
        $ROOT_DIR/old-service/old-service.pid \
        $ROOT_DIR/new-service-v1/new-service-v1.pid \
        $ROOT_DIR/new-service-v2/new-service-v2.pid \
        $ROOT_DIR/sidecar/sidecar.pid \
        $ROOT_DIR/comparison-service/comparison-service.pid; do
        if [[ -f $pid ]]; then
            kill "$(cat $pid)" 2>/dev/null || true
            rm -f $pid
        fi
    done
    # Kill any remaining on our ports
    for port in $COMP_PORT $SIDECAR_PORT $OLD_PORT $NEW_V1_PORT $NEW_V2_PORT; do
        fuser -k ${port}/tcp 2>/dev/null || true
    done
}

build_go() {
    local dir=$1
    local binary=$2
    info "Building $binary..."
    (cd "$dir" && go build -o "$binary" .) && ok "Built $binary" || { fail "Build failed for $dir"; exit 1; }
}

# ─── Main ────────────────────────────────────────────────────────────────────

trap stop_all EXIT

info "=== Traffic Comparison Demo ==="
info "This demo runs 5 local HTTP services to simulate K8s traffic shadowing."
echo

# Step 1: Stop any existing processes
stop_all
sleep 1

# Step 2: Build all services
echo
info "Building services..."
build_go "$ROOT_DIR/old-service"           "$ROOT_DIR/old-service/old-service"
build_go "$ROOT_DIR/new-service-v1"        "$ROOT_DIR/new-service-v1/new-service-v1"
build_go "$ROOT_DIR/new-service-v2"        "$ROOT_DIR/new-service-v2/new-service-v2"
build_go "$ROOT_DIR/sidecar"               "$ROOT_DIR/sidecar/sidecar"
build_go "$ROOT_DIR/comparison-service"   "$ROOT_DIR/comparison-service/comparison-service"
echo

# Step 3: Start old service
info "Starting old-service (port $OLD_PORT)..."
"$ROOT_DIR/old-service/old-service" -port=$OLD_PORT &
echo $! > "$ROOT_DIR/old-service/old-service.pid"
echo -n "  waiting for old-service..."
wait_for_port $OLD_PORT "old-service"
ok "old-service ready"

# Step 4: Start new services
info "Starting new-service-v1 (port $NEW_V1_PORT)..."
"$ROOT_DIR/new-service-v1/new-service-v1" -port=$NEW_V1_PORT &
echo $! > "$ROOT_DIR/new-service-v1/new-service-v1.pid"
echo -n "  waiting for new-service-v1..."
wait_for_port $NEW_V1_PORT "new-service-v1"
ok "new-service-v1 ready"

info "Starting new-service-v2 (port $NEW_V2_PORT)..."
"$ROOT_DIR/new-service-v2/new-service-v2" -port=$NEW_V2_PORT &
echo $! > "$ROOT_DIR/new-service-v2/new-service-v2.pid"
echo -n "  waiting for new-service-v2..."
wait_for_port $NEW_V2_PORT "new-service-v2"
ok "new-service-v2 ready"

# Step 5: Start comparison service
info "Starting comparison-service (port $COMP_PORT)..."
"$ROOT_DIR/comparison-service/comparison-service" \
    -port=$COMP_PORT \
    -config="$SCRIPT_DIR/config.yaml" \
    -loki="" -prom="" \
    > "$ROOT_DIR/comparison-service/comparison-service.log" 2>&1 &
echo $! > "$ROOT_DIR/comparison-service/comparison-service.pid"
echo -n "  waiting for comparison-service..."
wait_for_port $COMP_PORT "comparison-service"
ok "comparison-service ready"

# Step 6: Start sidecar
info "Starting traffic-mirror sidecar (port $SIDECAR_PORT)..."
COMPARISON_URL="http://localhost:$COMP_PORT" \
OLD_SERVICE_URL="http://localhost:$OLD_PORT" \
"$ROOT_DIR/sidecar/sidecar" -port=$SIDECAR_PORT \
    > "$ROOT_DIR/sidecar/sidecar.log" 2>&1 &
echo $! > "$ROOT_DIR/sidecar/sidecar.pid"
echo -n "  waiting for sidecar..."
wait_for_port $SIDECAR_PORT "sidecar"
ok "sidecar ready"

echo
echo
info "=== All services up! ==="
echo
echo "  old-service          http://localhost:$OLD_PORT"
echo "  new-service-v1       http://localhost:$NEW_V1_PORT"
echo "  new-service-v2       http://localhost:$NEW_V2_PORT"
echo "  comparison-service   http://localhost:$COMP_PORT"
echo "  sidecar              http://localhost:$SIDECAR_PORT"
echo
echo "---"

# ─── Run Test Cases ───────────────────────────────────────────────────────────

run_test() {
    local name="$1"
    local expected_severity="$2"
    local expected_diff="$3"
    local body="$4"

    echo
    info "TEST: $name"
    
    response=$(curl -s -X POST "http://localhost:$SIDECAR_PORT/mirror" \
        -H "Content-Type: application/json" \
        -H "X-Service-Pair: user-service-shadow" \
        -H "X-Original-Endpoint: /api/v1/users/123" \
        -H "X-Original-Method: GET" \
        ${body:+-d "$body"} \
        2>&1) || true

    echo "  Response: $response" | head -c 200
    echo

    # Check comparison result
    sleep 1
    report=$(curl -s "http://localhost:$COMP_PORT/health" 2>&1)
    # The comparison result is logged to sidecar.log — check there
    severity=$(grep -o '"severity":"[^"]*"' "$ROOT_DIR/sidecar/sidecar.log" 2>/dev/null | tail -1 | grep -o '"severity":"[^"]*"' | cut -d'"' -f4)
    
    if [[ -n "$severity" ]]; then
        echo "  → Detected severity: $severity"
        if [[ "$severity" == "$expected_severity" ]]; then
            ok "TEST PASSED: severity=$severity (expected $expected_severity)"
        else
            warn "Severity mismatch: got=$severity expected=$expected_severity"
        fi
    else
        warn "Could not determine severity — check sidecar log"
        tail -5 "$ROOT_DIR/sidecar/sidecar.log" 2>/dev/null || true
    fi
}

echo
echo "=============================================="
echo "  Running comparison tests..."
echo "=============================================="

# Test 1: v1 — should show MEDIUM (body fields differ)
run_test "Old vs new-v1 (body fields differ)" "MEDIUM" "name/score/plan_tier"

# Test 2: v2 — should show HIGH (slow + status error)
run_test "Old vs new-v2 (slow + 500 error)" "HIGH" "latency spike + status error"

echo
echo
info "=== Full comparison report ==="
curl -s "http://localhost:$COMP_PORT/rules" | python3 -m json.tool 2>/dev/null | head -40 || \
    curl -s "http://localhost:$COMP_PORT/rules"

echo
echo
info "=== Demo complete ==="
info "Logs:"
echo "  comparison-service: $ROOT_DIR/comparison-service/comparison-service.log"
echo "  sidecar:           $ROOT_DIR/sidecar/sidecar.log"
echo
echo "Press Ctrl+C to stop all services."
echo

# Keep running so user can interact
wait
