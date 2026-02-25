# OrbitPay Secrets Infrastructure

Secrets-hardened deployment environment for OrbitPay's payment-gateway-connector service with zero-downtime credential rotation, centralized secrets management, and Prometheus-based monitoring.

## Problem and Solution

| Problem | Solution |
|---------|----------|
| Credentials scattered across env vars, config files, and production servers with no centralized store | HashiCorp Vault (KV v2) as the single source of truth for all payment gateway credentials, with fine-grained HCL policies restricting access per service |
| Rotating VaultGateway credentials requires a service restart, causing 30-60 seconds of transaction failures during peak hours | Dual-credential polling: CredentialManager detects new secrets from Vault every 30 seconds, loads them alongside old credentials, and GatewayClient falls back automatically on auth failure. Zero restarts, zero dropped transactions |
| No visibility into credential health, expiration, or access patterns | Prometheus metrics (credential version, age, rotation count, auth failure rate, circuit breaker state), AlertManager rules for critical scenarios, Grafana dashboards, and structured JSON audit logging for every credential access |

## Architecture

```
┌─────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│   Client     │────>│ payment-gateway-     │────>│  VaultGateway   │
│   Request    │     │ connector            │     │  (mock)         │
└─────────────┘     │  ┌─────────────────┐ │     └─────────────────┘
                    │  │ Credential Mgr  │ │
                    │  │ (dual-cred)     │ │
                    │  └────────┬────────┘ │
                    └───────────┼──────────┘
                                │
                    ┌───────────v──────────┐
                    │   HashiCorp Vault    │
                    │   (KV v2 engine)     │
                    └──────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        v                       v                       v
┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐
│  Prometheus  │    │   AlertManager   │    │     Grafana      │
│  (metrics)   │───>│  (routing)       │    │  (dashboards)    │
└──────────────┘    └──────────────────┘    └──────────────────┘
```

**Data flow:**
1. Client sends a payment authorization request to the payment-gateway-connector.
2. The connector authenticates the request, then authorizes against VaultGateway using credentials loaded from Vault.
3. CredentialManager polls Vault every 30 seconds for credential updates. On rotation, both old and new credentials are held in memory (dual-credential window).
4. GatewayClient tries the primary (newest) credential. On 401/403, it falls back to the previous credential. This covers the window where VaultGateway may not have activated the new key yet.
5. Prometheus scrapes `/metrics` from the payment service and VaultGateway mock. AlertManager fires alerts on credential expiration, authentication failures, and circuit breaker state changes.

## Tech Stack

| Component | Tool | Justification |
|-----------|------|---------------|
| Orchestration | kind (Kubernetes in Docker) | NetworkPolicies, rolling updates with readiness probes, RBAC, production parity. Free, multi-node, all K8s features. |
| Secrets Manager | HashiCorp Vault (OSS, KV v2) | Audit trail, fine-grained HCL policies, secret versioning with rollback, dynamic secrets path. PCI-DSS Requirement 10 compliant. |
| Payment Service | Python FastAPI | Async I/O for non-blocking gateway calls, native Prometheus client, Pydantic request validation. |
| Gateway Mock | Python FastAPI | Simulates VaultGateway payment authorization API with configurable latency and API key validation. |
| Monitoring | Prometheus + Grafana + AlertManager | Plain K8s manifests (no Helm), full control over scrape configs and alert rules, zero vendor lock-in. |
| CI/CD | GitHub Actions | All actions pinned to specific versions. Lint, test, build, security scan, manifest validation. |

## Directory Structure

```
.
├── .github/
│   └── workflows/
│       └── ci.yml                  # GitHub Actions: lint, test, build, security scan
├── k8s/
│   ├── namespace.yaml              # orbitpay namespace (restricted PSS)
│   ├── vault/
│   │   ├── statefulset.yaml        # Vault server (dev mode)
│   │   ├── service.yaml            # ClusterIP service on port 8200
│   │   ├── configmap.yaml          # HCL policy + init script
│   │   ├── secret.yaml             # Dev root token (placeholder)
│   │   ├── networkpolicy.yaml      # Ingress from payment service only
│   │   └── init-job.yaml           # One-shot: enable KV v2, write policy, seed creds
│   ├── payment-gateway-connector/
│   │   ├── deployment.yaml         # 2 replicas, rolling update, security context
│   │   ├── service.yaml            # ClusterIP on port 8000
│   │   ├── configmap.yaml          # Vault address, secret path, poll interval
│   │   ├── secret.yaml             # Vault token (dev placeholder)
│   │   ├── serviceaccount.yaml     # Dedicated SA, automount disabled
│   │   └── networkpolicy.yaml      # Egress to Vault + VaultGateway only
│   ├── vaultgateway-mock/
│   │   ├── configmap.yaml          # Server config (port, latency)
│   │   └── secret.yaml             # Accepted API keys (dev placeholder)
│   └── monitoring/
│       ├── prometheus/              # Scrape config, alert rules, deployment
│       ├── alertmanager/            # Routing, receivers, deployment
│       └── grafana/                 # Data sources, dashboards, deployment
├── services/
│   ├── payment-gateway-connector/
│   │   ├── app/
│   │   │   ├── config.py           # Pydantic settings (env vars, no secrets)
│   │   │   ├── vault_client.py     # hvac wrapper with retry + audit
│   │   │   ├── credential_manager.py # Dual-cred cache, polling, metrics
│   │   │   ├── gateway_client.py   # Circuit breaker, fallback auth
│   │   │   ├── models.py           # Pydantic request/response models
│   │   │   ├── middleware.py       # Request ID, audit logging, rate limiting
│   │   │   └── audit.py           # Structured JSON audit logger
│   │   ├── tests/                  # pytest with mocked Vault + gateway
│   │   ├── Dockerfile              # Multi-stage, non-root, minimal base
│   │   └── requirements.txt
│   └── vaultgateway-mock/
│       ├── app/                    # Mock gateway with API key validation
│       └── Dockerfile              # Multi-stage, non-root
├── scripts/
│   ├── setup.sh                    # Full bootstrap (kind + build + deploy)
│   ├── teardown.sh                 # Cluster deletion + cleanup
│   └── rotate-credentials.sh      # Credential rotation demo
├── DESIGN.md                       # Decision diary, trade-offs, cost analysis
├── SETUP.md                        # Step-by-step setup + troubleshooting
└── README.md                       # This file
```

## Security Controls

### Implemented (real enforcement)

- **Vault policies:** `payment-gateway-connector` has read-only access to `secret/data/orbitpay/vaultgateway`. No create, update, or delete capabilities. Principle of least privilege.
- **NetworkPolicies:** Strict ingress/egress per pod. Vault accepts traffic only from the payment service and init job. Payment service can reach only Vault and VaultGateway. All other traffic denied by default.
- **Non-root containers:** All application containers run as UID 1000 with `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, and all capabilities dropped.
- **Pod Security Standards:** Namespace enforces `restricted` PSS via `pod-security.kubernetes.io/enforce: restricted`. Pods that violate restricted criteria are rejected by the admission controller.
- **seccomp profiles:** `RuntimeDefault` seccomp profile on all containers.
- **No secrets in code or logs:** Credentials loaded from Vault at runtime via `hvac`. Audit logger emits only secret paths and version numbers, never values.
- **API documentation disabled:** No Swagger UI, no ReDoc, no OpenAPI endpoint exposed.
- **Fail-closed:** Service returns 503 if no valid credentials are available. Readiness probe fails, removing the pod from the Service.
- **Bounded data structures:** Credential cache capped at 5 entries (OrderedDict with LRU eviction). Rate limiter buckets capped at 10,000.
- **Circuit breaker:** Opens after 5 consecutive gateway failures, fast-fails subsequent requests (no cascading timeouts).
- **Structured audit logging:** Every credential access, rotation, and auth failure produces a JSON audit event.

### Simulated (production gaps documented)

- Vault runs in dev mode (in-memory, no TLS, no HA). Production: 3-node HA with Raft, TLS, cloud KMS auto-unseal.
- Vault token auth used. Production: Kubernetes ServiceAccount auth method.
- K8s Secrets store the Vault token. Production: eliminate static tokens via K8s auth.
- No mTLS between services. Production: service mesh (Istio/Linkerd).
- No etcd encryption at rest. Production: EncryptionConfiguration with AES-CBC or KMS provider.
- AlertManager webhooks are placeholders. Production: real PagerDuty/Slack receivers.

See [DESIGN.md](DESIGN.md) Section 4 for full details on each trade-off.

## Quick Start

### Prerequisites

- Docker Desktop 24+
- kind 0.20+
- kubectl 1.28+
- ~4 GB free RAM, ~10 GB disk

### Automated Setup

```bash
./scripts/setup.sh
```

### Test a Payment

```bash
curl -s -X POST http://localhost:8000/api/v1/payments/authorize \
  -H "Content-Type: application/json" \
  -H "X-API-Key: default-api-key" \
  -d '{
    "merchant_id": "merchant-001",
    "amount": 99.99,
    "currency": "USD",
    "card_token": "tok_test_visa_4242",
    "idempotency_key": "idem-001"
  }'
```

### Rotate Credentials (Zero Downtime)

```bash
# Write new credential to Vault
kubectl -n orbitpay exec vault-0 -- vault kv put secret/orbitpay/vaultgateway \
  api_key="ogw-rotated-key-$(date +%s)" \
  api_secret="ogw-rotated-secret-$(date +%s)" \
  merchant_id="orbitpay-merchant-001" \
  gateway_url="http://vaultgateway-mock.orbitpay.svc.cluster.local:8080" \
  created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  version="2"

# Wait ~30 seconds for CredentialManager to detect the change

# Verify rotation was detected
curl -s http://localhost:8000/metrics | grep credential_rotation_total

# Send a payment to confirm it works with the new credential
curl -s -X POST http://localhost:8000/api/v1/payments/authorize \
  -H "Content-Type: application/json" \
  -H "X-API-Key: default-api-key" \
  -d '{
    "merchant_id": "merchant-002",
    "amount": 25.00,
    "currency": "EUR",
    "card_token": "tok_test_mastercard_5555",
    "idempotency_key": "idem-rotation-001"
  }'
```

No service restart. No dropped transactions. The CredentialManager detected the new version, loaded it alongside the old credential, and the GatewayClient used the new credential for subsequent requests.

### Teardown

```bash
./scripts/teardown.sh
# Or: kind delete cluster --name orbitpay
```

## Monitoring and Alerts

### Key Metrics

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `credential_version` | Current credential version from Vault | N/A (informational) |
| `credential_age_seconds` | Age of current credential | > 86400 (24h) triggers warning |
| `credential_rotation_total` | Count of detected rotations | N/A (informational) |
| `credential_fetch_errors_total` | Vault read failures | > 0 sustained for 2 min |
| `gateway_requests_total{status}` | Gateway requests by HTTP status | 401/403 rate > 10% over 5 min |
| `gateway_auth_failures_total` | All-credential auth failures | > 0 triggers critical |
| `circuit_breaker_state` | 0=closed, 1=open, 2=half_open | = 1 (open) triggers critical |
| `gateway_request_duration_seconds` | Gateway latency histogram | p99 > 5s triggers warning |

### Accessing Dashboards

After port-forwarding (see [SETUP.md](SETUP.md) Step 13):

- **Prometheus:** http://localhost:9090 -- query metrics, check targets, view alert rules
- **Grafana:** http://localhost:3000 (admin/admin) -- pre-configured dashboards for credential health and gateway performance
- **AlertManager:** http://localhost:9093 -- view active alerts, silence, and routing

## CI/CD Pipeline

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs on every push and PR to `master`/`main`:

1. **lint:** Runs `ruff check` on both Python services
2. **test:** Installs dependencies and runs `pytest` with coverage (requires lint to pass)
3. **build:** Builds Docker images for both services (requires test to pass)
4. **security-scan:** Checks for plaintext secrets in code, verifies Dockerfiles use `USER` directive, verifies K8s manifests have `securityContext` (requires build to pass)
5. **validate-manifests:** Validates all K8s YAML files parse correctly (runs in parallel with lint)

All GitHub Actions are pinned to specific versions (e.g., `actions/checkout@v4.1.1`, `actions/setup-python@v5.3.0`).

## Documentation

- **[DESIGN.md](DESIGN.md):** Decision diary with tool rationale, zero-downtime rotation strategy, security trade-offs, failure scenarios, cost analysis, and production readiness gaps.
- **[SETUP.md](SETUP.md):** Step-by-step setup guide with 15 manual steps, troubleshooting for 6 common scenarios, and teardown instructions.
