# Traffic Comparison Service — SPEC

## 1. Overview

**Name**: traffic-comparison
**Type**: K8s traffic shadowing + multi-target diff service
**Core Function**: Mirror traffic from a production (old) service to new service(s), compare responses, and alert on differences.
**Stage**: v1 Demo (local, no K8s required)

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Test / Client                                                   │
│  curl -X POST :8090/mirror  ← sidecar HTTP mirror endpoint      │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  traffic-mirror-sidecar (:8090)                                 │
│  - Receives mirrored HTTP requests                               │
│  - Forwards to old-service (real target)                         │
│  - Returns old-service response to caller (zero latency impact)  │
│  - Async: POST /compare to comparison-service (fire & forget)   │
└────────────────────────┬────────────────────────────────────────┘
                         │ async POST /compare
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  comparison-service (:8080)                                      │
│  - Receives ShadowPayload (old service response)               │
│  - Concurrently requests ALL configured targets                 │
│  - Computes structured diff (status / headers / body)          │
│  - Evaluates severity (HIGH / MEDIUM / LOW / NONE)             │
│  - Reports to Grafana Loki (async, non-blocking)                │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         │ diff reports
                         ▼
                  ┌──────────────┐
                  │   Grafana    │
                  │    Loki      │
                  └──────────────┘

Services (模拟):
  old-service      :9001  — baseline "production" service
  new-service-v1   :9002  — v2.0.0, some fields differ
  new-service-v2   :9003  — v3.1.0, slow + intermittent 500
```

---

## 3. Features

- [x] Multi-target comparison (N targets per rule, concurrent requests)
- [x] Weighted target selection (weight field, for future canary %)
- [x] Structured JSON diff (path-aware, not raw text diff)
- [x] Configurable ignore fields (timestamp, requestId, etc.)
- [x] Severity classification (HIGH / MEDIUM / LOW / NONE)
- [x] Grafana Loki integration (log stream per target)
- [x] Prometheus pushgateway integration (counters + histograms)
- [x] Async sidecar (fire & forget, 3s timeout, circuit breaker)
- [x] Health endpoint for all services
- [x] Demo test script (runs full stack locally, no K8s required)

---

## 4. API

### 4.1 Sidecar — Mirror Request

```
POST :8090/mirror
Headers:
  X-Service-Pair: user-service-shadow     (required — rule name)
  X-Original-Endpoint: /api/v1/users/123  (required — original path)
  X-Original-Method: GET                   (default: GET)
  Content-Type: application/json
Body: <original request body if any>

Response: mirror of old-service response (unchanged)
Side effect: async POST /compare to comparison-service
```

### 4.2 Comparison Service

```
POST /compare
Body: ShadowPayload (JSON)
Response: CompareResponse (multi-target diff result)

GET /health
Response: {"status": "ok"}

GET /rules
Response: ServiceConfig (all loaded rules)
```

---

## 5. Data Models

### CompareRequest (sidecar → comparison-service)
```json
{
  "id": "uuid",
  "service_pair": "user-service-shadow",
  "endpoint": "/api/v1/users/123",
  "method": "GET",
  "timestamp": "2026-05-18T23:00:00Z",
  "shadow": {
    "status_code": 200,
    "headers": {"content-type": "application/json", ...},
    "body_b64": "eyJkb2NzIjpbXX0=",
    "latency_ms": 45
  }
}
```

### CompareResponse (comparison-service → client + Loki)
```json
{
  "report_id": "uuid",
  "service_pair": "user-service-shadow",
  "endpoint": "/api/v1/users/123",
  "method": "GET",
  "severity": "MEDIUM",
  "shadow": { "status_code": 200, "latency_ms": 45, "body_len": 342 },
  "targets": [
    {
      "target_name": "new-v1",
      "target_version": "v2.0.0",
      "url": "http://localhost:9002/api/v1/users/123",
      "status_code": 200,
      "latency_ms": 68,
      "status_diff": false,
      "header_diff": [
        {"path": "cache-control", "old_value": "no-cache", "new_value": "no-store"}
      ],
      "body_diff": [
        {"path": "data[0].name", "old_value": "Alice", "new_value": "Alicia"},
        {"path": "data[0].score", "old_value": "95.5", "new_value": "96"}
      ],
      "latency_diff_ms": 23,
      "severity": "MEDIUM"
    },
    {
      "target_name": "new-v2",
      "target_version": "v3.1.0",
      "url": "http://localhost:9003/api/v1/users/123",
      "status_code": 500,
      "latency_ms": 2145,
      "status_diff": true,
      "severity": "HIGH",
      "error": ""
    }
  ],
  "compared_at": "2026-05-18T23:00:00.123Z"
}
```

---

## 6. Severity Rules

| Condition | Severity |
|-----------|----------|
| Target request fails / unreachable | HIGH |
| HTTP status code differs | HIGH |
| >5 body fields differ | HIGH |
| >3 header fields differ | HIGH |
| Latency diff >5s | HIGH |
| Any body field differs | MEDIUM |
| Any header field differs | MEDIUM |
| Latency diff >1s | MEDIUM |
| Latency diff >200ms | LOW |
| No differences | NONE |

Overall severity = highest among all targets.

---

## 7. Config Format (YAML)

```yaml
comparison_service_url: "http://localhost:8080"

rules:
  - name: user-service-shadow
    old_service: old-service
    namespace: default
    targets:
      - name: new-v1
        url: http://localhost:9002
        weight: 50
        version: "v2.0.0"
      - name: new-v2
        url: http://localhost:9003
        weight: 50
        version: "v3.1.0"
    endpoints:
      - path: "/api/v1/users/"
        method: GET
        ignore_fields:
          - "$.timestamp"
          - "$.request_id"
          - "$.data[0].score"
    sampling_rate: 1.0
    timeout_ms: 5000
```

---

## 8. File Structure

```
traffic-comparison/
├── SPEC.md
├── Makefile
│
├── comparison-service/           # Go — 对比服务核心
│   ├── main.go
│   ├── go.mod
│   ├── types/types.go
│   ├── handler/compare.go
│   ├── diff/
│   │   ├── json_diff.go          # 结构感知 JSON diff
│   │   └── severity.go           # 严重等级判定
│   ├── grafana/client.go         # Loki + Prometheus 上报
│   └── config/loader.go          # YAML 配置加载
│
├── old-service/                  # Go — 模拟老服务
│   ├── main.go                   # v1.2.3 baseline responses
│   └── go.mod
│
├── new-service-v1/               # Go — 模拟新服务 v2
│   ├── main.go                   # v2.0.0 — 部分字段差异
│   └── go.mod
│
├── new-service-v2/               # Go — 模拟新服务 v3
│   ├── main.go                   # v3.1.0 — 慢 + 500 错误
│   └── go.mod
│
├── sidecar/                      # Go — 轻量 sidecar
│   ├── main.go                   # HTTP mirror + async reporter
│   └── go.mod
│
└── demo/
    ├── config.yaml               # 对比规则配置
    └── test.sh                   # 本地 Demo 测试脚本
```

---

## 9. Running the Demo

```bash
cd traffic-comparison

# Build all services
make build

# Run full demo (interactive, Ctrl+C to stop)
make run-demo

# Or manually:
./demo/test.sh
```

Expected output:
```
[ OK ] old-service ready
[ OK ] new-service-v1 ready
[ OK ] new-service-v2 ready
[ OK ] comparison-service ready
[ OK ] sidecar ready

TEST: Old vs new-v1 (body fields differ)
  → Detected severity: MEDIUM

TEST: Old vs new-v2 (slow + 500 error)
  → Detected severity: HIGH
```

---

## 10. Future Work

| Priority | Feature |
|----------|---------|
| P0 | K8s MutatingWebhook for automatic sidecar injection |
| P1 | gRPC support |
| P1 | Canary traffic split (weight-based routing) |
| P2 | HTML diff report generator |
| P2 | Multi-cluster federation |
| P3 | Automated regression analysis (trend over time) |
