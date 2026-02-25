# OrbitPay Secrets Infrastructure

## Language
All code, comments, documentation, commit messages, and variable names MUST be in **English**. No exceptions.

## Project Overview
Secrets-hardened deployment environment for OrbitPay's payment-gateway-connector service with zero-downtime credential rotation, monitoring, and PCI-DSS aligned security controls.

## Tech Stack

| Component | Tool | Why |
|-----------|------|-----|
| Orchestration | kind (multi-node K8s) | Production-like, supports NetworkPolicies, rolling updates, probes |
| Secrets Manager | HashiCorp Vault (in-cluster) | Rotation, audit trail, fine-grained policies, KV v2 engine |
| Payment Service | Python FastAPI | Async, lightweight, prometheus client, health endpoints |
| Gateway Mock | Python FastAPI | Simulates VaultGateway payment API |
| Monitoring | Prometheus + Grafana + AlertManager | Plain K8s manifests, no Helm |
| CI/CD | GitHub Actions | Pinned to SHA, multi-stage Docker builds |

## Directory Layout
- `services/payment-gateway-connector/` — Payment authorization service with Vault integration
- `services/vaultgateway-mock/` — Mock payment gateway (VaultGateway simulator)
- `k8s/` — Kubernetes manifests organized by component
- `k8s/vault/` — Vault StatefulSet, service, config
- `k8s/monitoring/` — Prometheus, Grafana, AlertManager manifests
- `scripts/` — Bootstrap, teardown, rotation, demo scripts
- `.github/workflows/` — CI/CD pipeline

## Commit Conventions
- Conventional Commits: `type(scope): description`
- Types: `feat`, `fix`, `docs`, `chore`, `ci`, `test`, `refactor`
- Each commit = one epic. Minimum 12 commits. Push at the end.

## Code Standards (NEVER violate)
- **Python**: Type hints, async, `ruff` clean, `@pytest.mark.asyncio` on all async tests
- **Dockerfiles**: Multi-stage, non-root (`USER 1000`), minimal base
- **K8s**: Resource limits, probes, `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false`
- **Scripts**: `set -euo pipefail`, `command -v` checks, colored output, trap cleanup
- **Secrets**: Never in code, env vars, logs, or git
- **API docs**: Disabled in production services (`docs_url=None, redoc_url=None, openapi_url=None`)
- **Data structures**: Bounded with eviction, never unbounded
- **Audit logging**: Dedicated `audit` logger with structured JSON
- **Error handling**: Fail-closed always
- **Tests**: Isolated `CollectorRegistry` per test, mock transports, no real network calls

## Architecture
- Vault runs as StatefulSet in `orbitpay` namespace with KV v2 secrets engine
- Payment service reads credentials from Vault via `hvac` Python client
- Credential manager watches for changes, supports dual-credential validation window
- Graceful shutdown with connection draining for zero-downtime rotation
- Prometheus scrapes `/metrics` from all services; AlertManager routes by severity
- NetworkPolicies enforce least-privilege network access per pod

## Testing
- pytest with realistic mocked data
- Cover: happy path, error cases, fallback behavior, edge cases
- E2E demo script validates the main rotation flow

## Documentation
- `DESIGN.md`: Decision diary, trade-offs, cost analysis, failure scenarios, OWASP findings
- `SETUP.md`: Prerequisites, quick start, manual steps, troubleshooting, teardown
- `README.md`: Problem→Solution, architecture diagram, security controls, demo instructions
