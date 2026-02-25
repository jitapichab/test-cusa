# OrbitPay Secrets Infrastructure -- Design Decisions

## 1. Executive Summary

OrbitPay processes payment transactions across 12 countries and depends on VaultGateway for authorization. A potential credential exposure incident revealed that our secrets management was ad-hoc, rotation required downtime, and we had zero visibility into credential health. This document captures the decisions, trade-offs, and mistakes I made while building a reference implementation that solves all three problems: centralized secrets with fine-grained policies, zero-downtime dual-credential rotation, and Prometheus-based monitoring with actionable alerts for credential failures.

## 2. Tool Choices and Rationale

### Secrets Manager: HashiCorp Vault (OSS, in-cluster, dev mode)

I evaluated five options seriously before landing on Vault:

| Option | Pros | Cons | Why not |
|--------|------|------|---------|
| **AWS Secrets Manager** | Managed, $0.40/secret/month, auto-rotation via Lambda | Vendor lock-in, no local dev story without LocalStack, ~$20/mo for our ~50 secrets | Ruled out: no local kind-compatible path without cloud credentials |
| **SOPS + age** | Zero infra, git-friendly, encrypted at rest | No audit trail, no dynamic secrets, no built-in rotation, no API for runtime reads | Ruled out: solves storage but not rotation or audit |
| **K8s Sealed Secrets** | Cluster-native, GitOps friendly | Secrets land as plain K8s Secrets in etcd (not encrypted by default), no rotation workflow, no audit API | Ruled out: doesn't address the core rotation problem |
| **External Secrets Operator** | Bridges K8s to external stores, good for multi-cloud | Still needs a backend store (Vault, ASM, etc.), adds operator complexity, chicken-and-egg for this exercise | Ruled out: adds a layer without solving the backend question |
| **HashiCorp Vault** | Audit trail, fine-grained HCL policies, KV v2 versioning, dynamic secrets path, active community | Operational complexity, requires unsealing (or dev mode), resource overhead | **Chose this** |

**Cost reality check:** Vault OSS is free, but running it in HA for production costs roughly $500-800/month (2 nodes on c5.large or equivalent, plus Raft/Consul storage). AWS Secrets Manager for our ~50 secrets would run about $20/month plus $0.05 per 10K API calls. The cost difference is real. I chose Vault because for a payment system, the audit trail and versioned secrets (KV v2 gives us version history for every credential change) justify the operational cost. PCI-DSS Requirement 10 essentially demands this level of access logging.

For this exercise specifically, Vault runs in dev mode (in-memory, no TLS, single node). That is the right call for a local demo, but I want to be clear: dev mode is not production. Section 4 details exactly what changes.

### Container Orchestration: kind (Kubernetes in Docker)

Why not Docker Compose? Three reasons:

1. **NetworkPolicies** -- Compose has no equivalent. I wanted to demonstrate actual network segmentation where Vault only accepts traffic from the payment service and the init job. Compose networks are flat.
2. **Rolling updates with maxUnavailable: 0** -- Compose V2 has `deploy.update_config` but it doesn't integrate with health checks the same way. K8s readiness probes gate traffic routing, which is critical for zero-downtime rotation.
3. **Production parity** -- If this is a blueprint for production, it should use the same primitives (Deployments, Services, ServiceAccounts, securityContext) that production will use.

kind specifically because it is free, supports multi-node clusters, handles all K8s features including NetworkPolicies (with a CNI like Kindnet or Calico), and works in CI. minikube would also work but kind's `kind load docker-image` workflow is cleaner for local image testing.

### Monitoring: Prometheus + Grafana + AlertManager (plain manifests)

I considered Datadog ($15/host/month for Infrastructure, $23/host for APM) and New Relic (similar pricing). For 4-6 pods in a dev cluster, the cost would be modest, but vendor lock-in on alerting rules and dashboard definitions is the real problem. If we move clouds or change vendors, we lose all our SLO definitions.

I also deliberately avoided Helm for the monitoring stack. Helm charts for kube-prometheus-stack are 500+ lines of values.yaml and pull in dozens of CRDs. For this exercise, I wanted every line of configuration to be visible and explainable. The trade-off is more YAML to maintain (roughly 200 lines for Prometheus + AlertManager + Grafana vs. a single `helm install`), but the benefit is that every alert rule, every scrape config, every Grafana data source is in a plain file that anyone can read without knowing Helm.

### Application: Python FastAPI

The payment-gateway-connector needed to be async (payment gateway calls are I/O bound, we cannot block a worker thread per request), have solid Prometheus integration (`prometheus_client` just works), and support Pydantic for request validation (card tokens, amounts, currency codes all need strict validation). FastAPI checks all boxes. Django would be overkill for a single-endpoint service. Flask could work but lacks native async support without additional workarounds.

## 3. Zero-Downtime Credential Rotation Strategy

This is the core of the exercise. Here is the exact sequence of what happens when VaultGateway issues new credentials:

### Step-by-step rotation sequence

```
Time    Event                                          State
─────   ──────────────────────────────────────────     ─────────────────────
T+0     Ops engineer (or automation) writes new        Vault KV v2 gets
        credential to Vault KV v2. Old credential      version N+1.
        remains as version N.                          Service still using
                                                       version N.

T+30s   CredentialManager's polling loop fires         Service detects
        (interval: 30s). Reads latest secret from      version change.
        Vault. Detects version N -> N+1.               Loads N+1 into
                                                       memory alongside N.

T+30s   credential_rotation_total metric increments.   Dual-credential
        Audit log emits "credential_rotation" event    window begins.
        with old_version=N, new_version=N+1.           Both N and N+1
        Health endpoint reports                        are available.
        "credential_status": "valid".

T+30s+  Payment request arrives. GatewayClient tries   Primary: version N+1
        primary credential (N+1) first.                Fallback: version N

        If N+1 succeeds -> great, continue.
        If N+1 gets 401/403 -> automatically tries
        N (the fallback). This handles the case where
        VaultGateway hasn't activated the new key yet.

T+5m    Grace period expires (configurable via         Old credential N
        MAX_CACHE_SIZE eviction). Credential N         available as long
        may be evicted from the bounded OrderedDict    as cache has room.
        if newer credentials arrive.                   Max 5 entries.

T+any   VaultGateway admin revokes old API key N.      Service exclusively
        Service is already using N+1. No impact.       uses N+1.
```

### Why this approach over alternatives

**Blue-green deployment for rotation:** This would mean maintaining two complete deployments (blue and green), updating green with new credentials, switching the Service selector, and draining blue. It works, but it doubles the resource footprint during rotation and introduces a Service selector switch that is itself a failure point. For credential rotation alone (not code deployment), this is overkill.

**Rolling restart with new env vars:** The "traditional" approach: update a Secret, do `kubectl rollout restart`. During the restart window (with 2 replicas, maxSurge=1, maxUnavailable=0), there is a period where the old pod is terminating and the new pod is starting. If the readiness probe takes 10 seconds and termination grace period is 45 seconds, we might see 5-10 seconds of reduced capacity. At 35 TPS (OrbitPay's peak), that is 175-350 transactions hitting a single pod instead of two. Not catastrophic, but avoidable.

**Our approach (dual-credential polling):** Zero application restarts. Zero pod cycling. The CredentialManager polls Vault every 30 seconds, loads the new credential into an in-memory cache alongside the old one, and the GatewayClient tries the new credential first with automatic fallback to the old one. The worst case is a 30-second window where the service hasn't picked up the new credential yet, during which it continues using the old (still valid) credential. No connections are dropped. No pods restart.

The trade-off: we depend on the application code (CredentialManager + GatewayClient) implementing the dual-credential logic correctly. A rolling restart approach is "dumber" but works with any application. Our approach requires the service to be credential-rotation-aware. For a payment service, I think that investment is worthwhile.

## 4. Security Trade-offs: Real vs. Simulated

### Real security controls implemented

These are not placeholders. They are enforced in the K8s manifests and application code:

- **Vault policies:** `payment-gateway-connector` policy grants `read` on `secret/data/orbitpay/vaultgateway` and `read, list` on `secret/metadata/orbitpay/vaultgateway`. Nothing else. No `create`, no `update`, no `delete`. If the payment service is compromised, the attacker can read the current credentials but cannot modify them or access other paths.

- **NetworkPolicies:** Vault pods only accept ingress from pods labeled `app.kubernetes.io/name: payment-gateway-connector` and `app.kubernetes.io/name: vault-init` on port 8200. Payment-gateway-connector allows egress only to Vault (port 8200), VaultGateway mock (port 8080), and DNS. Prometheus can scrape the payment service (ingress on 8000 from `app.kubernetes.io/name: prometheus`). Everything else is denied by default.

- **Non-root containers:** Payment-gateway-connector runs as UID 1000 with `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, and all capabilities dropped. The only writable path is `/tmp` via an emptyDir.

- **No secrets in code, env vars baked into images, or logs:** Secrets are read from Vault at runtime via the `hvac` client. The audit logger (`app/audit.py`) explicitly logs only secret paths and version numbers, never values. The `SecretData` dataclass is never serialized to logs.

- **API docs disabled:** `docs_url=None, redoc_url=None, openapi_url=None` on the FastAPI app. No Swagger UI in production.

- **Fail-closed error handling:** If Vault is unreachable on startup and no cached credentials exist, the service refuses all payment requests (returns 503). It does not fall back to empty credentials or bypass authentication.

- **Structured audit logging:** Dedicated `audit` logger emits JSON events for every credential access, rotation, and authentication failure. This satisfies PCI-DSS Requirement 10.2 (audit trail for access to cardholder data environment components).

- **Bounded data structures:** The credential cache is an `OrderedDict` capped at 5 entries with LRU eviction. The rate limiter bucket map is capped at 10,000 entries. No unbounded growth.

- **Pod Security Standards:** The `orbitpay` namespace enforces the `restricted` Pod Security Standard (`pod-security.kubernetes.io/enforce: restricted`). Pods that don't meet restricted criteria are rejected by the admission controller.

- **seccomp profiles:** `RuntimeDefault` seccomp profile on all containers, reducing the syscall attack surface.

### Simulated / simplified for this exercise

I want to be honest about what is not production-ready:

- **Vault dev mode:** In-memory storage, no TLS, auto-unsealed with a root token. Production requires: HA cluster (3+ nodes) with Raft consensus, TLS on all Vault communication, auto-unseal via cloud KMS (AWS KMS, GCP Cloud KMS), and proper initialization with Shamir key shares.

- **K8s Secrets for the Vault token:** The payment service gets its Vault token from a K8s Secret (`payment-gateway-connector-secret`). In production, the service would use the Kubernetes auth method: the pod's ServiceAccount JWT is exchanged for a scoped Vault token, eliminating the need for a static token entirely.

- **Dev root token for init:** The `vault-init` Job uses the dev root token. Production: use a one-time init token with specific policy capabilities, or better, use the Vault Operator.

- **AlertManager webhooks are placeholders:** The AlertManager config routes alerts but the receiver endpoints are not connected to PagerDuty, Slack, or OpsGenie. Production requires real webhook URLs and escalation policies.

- **No mutual TLS between services:** Traffic between the payment service and VaultGateway mock is plain HTTP inside the cluster. Production: deploy a service mesh (Istio or Linkerd) for automatic mTLS between all pods, or use application-level TLS with cert-manager.

- **No EncryptionConfiguration for etcd:** K8s Secrets are stored in etcd as base64 (not encrypted at rest). Production requires `EncryptionConfiguration` with an AES-CBC or KMS provider to encrypt Secret resources at rest in etcd.

- **No image signing or SBOM:** Container images are built locally and loaded into kind. Production: sign images with cosign, generate SBOMs with syft, scan with Trivy, and enforce image policies with Kyverno or OPA Gatekeeper.

## 5. Failure Scenarios and Recovery

| Scenario | Detection | Impact | Recovery |
|----------|-----------|--------|----------|
| **Vault unavailable** | `VaultUnreachable` alert fires after 2 min of failed health checks. `credential_fetch_errors_total` counter increases. | Service continues processing using the last successfully cached credentials. New rotations cannot be detected. | Auto-recovery when Vault returns to service. CredentialManager retries with exponential backoff (0.5s, 1s, 2s). Cached credentials remain valid until their TTL expires. |
| **Expired credentials** | `CredentialExpired` alert fires when `credential_age_seconds` exceeds TTL. `credential_status` metric shows "expired". | VaultGateway rejects requests with 401. Service returns 503 to clients. | Run `rotate-credentials.sh` to write a fresh credential to Vault. CredentialManager picks it up within 30 seconds. Service auto-recovers without restart. |
| **Botched rotation (bad new cred)** | `GatewayAuthFailureRate` alert fires when 401/403 responses exceed 10% over 5 minutes. `gateway_auth_failures_total` counter spikes. | GatewayClient tries new credential (fails), automatically falls back to old credential (succeeds). Transactions continue on the old credential during the dual-credential window. | Revert the Vault secret to the previous version (`vault kv rollback -version=N secret/orbitpay/vaultgateway`). Service detects the rollback on next poll and updates accordingly. |
| **VaultGateway API down** | `CircuitBreakerOpen` alert fires when circuit breaker transitions to OPEN state. `circuit_breaker_state` metric = 1. `gateway_requests_total{status="timeout"}` increases. | Circuit breaker opens after 5 consecutive failures. All payment requests get fast-fail 503 (no waiting for timeouts). | Circuit breaker transitions to HALF_OPEN after 30 seconds, sends a probe request. If the probe succeeds, circuit closes and full traffic resumes. If it fails, circuit stays open for another 30 seconds. |
| **Payment service OOM** | Kubernetes `OOMKilled` event. Pod restarts automatically (restart count visible in `kubectl get pods`). `kube_pod_container_status_restarts_total` metric increases. | Brief unavailability for that replica (15-20 seconds for restart + readiness probe). Other replicas continue serving (we run 2 replicas). | K8s auto-restarts the pod. Resource limits (`256Mi` memory) prevent cascading. If repeated OOMs occur, investigate memory leak -- likely the bounded cache eviction is not working or a middleware is buffering unbounded request bodies. |
| **Network partition (pod isolated)** | Readiness probe (`/health/ready`) fails. K8s removes the pod from the Service endpoints. | Reduced capacity (1 of 2 replicas). Traffic routes exclusively to the healthy pod. | K8s readiness probe checks every 5 seconds with 3 failure threshold. Once the partition heals, the probe succeeds and K8s adds the pod back to the Service. No manual intervention needed. |
| **Prometheus scrape failure** | `up{job="payment-gateway-connector"} == 0` alert fires. Grafana dashboards show gaps. | Loss of observability (no metrics). The service itself is unaffected. | Check NetworkPolicy allows Prometheus to reach port 8000 on payment-gateway-connector pods. Verify Prometheus ServiceMonitor or scrape config targets. Common cause: label selector mismatch after redeployment. |

## 6. What I Got Wrong Initially

**Vault Agent Injector -- abandoned after day one.** My first design used the Vault Agent Injector sidecar to inject credentials as files into the payment service pod. The injector watches for annotations like `vault.hashicorp.com/agent-inject-secret-credentials: "secret/data/orbitpay/vaultgateway"` and writes the secret to a shared tmpfs volume. The problem: deploying the injector requires a MutatingAdmissionWebhook, TLS certificates for the webhook, and either Helm or a significant amount of manual YAML. Without Helm (which I deliberately excluded for transparency), the injector setup is roughly 400 lines of manifests plus cert generation. I switched to direct Vault API calls via the `hvac` Python library. The trade-off: the injector handles rotation automatically (watches for secret changes and rewrites the file), while my approach requires the application to poll Vault explicitly. But the polling approach is simpler, has no sidecar overhead, and the 30-second polling interval is acceptable for credential rotation (which happens on human timescales, not millisecond timescales).

**Single-credential model -- broke during mental simulation.** My initial CredentialManager had a single `_current_credential` field. When a new credential arrived, it overwrote the old one. Then I mentally walked through the rotation sequence: what happens if a payment request is in-flight using the old credential when the new one arrives? The HTTP call to VaultGateway has already been sent with the old API key header. That is fine -- in-flight requests complete with whatever credential they started with. But what about the next request that arrives in the 0-1 second window between "new credential written to Vault" and "VaultGateway has activated the new key"? The service would try the new key (not yet active), get a 401, and fail. Adding the dual-credential fallback (`get_fallback_credential()` + `_try_authorize` retry in GatewayClient) solved this. The old credential is kept in the cache and used as a fallback on 401/403 responses.

**Kubernetes auth -- right for production, wrong for this demo.** I initially designed the Vault client to use Kubernetes auth exclusively (the pod's ServiceAccount JWT is exchanged for a Vault token). In a kind cluster with dev-mode Vault, this requires: enabling the Kubernetes auth backend in Vault, configuring it with the K8s API server URL and CA cert, creating a role that maps the `payment-gateway-connector` ServiceAccount to a Vault policy, and setting `automountServiceAccountToken: true` on the pod. The last part conflicts with our security hardening (we set `automountServiceAccountToken: false` everywhere). Rather than create an exception, I used token auth for the demo and documented Kubernetes auth as the production path. The `VaultClient` class supports both methods (`vault_auth_method: "kubernetes"` or `"token"`) so the switch is a config change, not a code change.

**Unbounded idempotency key tracking -- caught during code review.** I initially used a plain `dict` to track idempotency keys for deduplication. In a payment service running for weeks, this dict would grow without bound. Switched to `OrderedDict` with LRU eviction capped at a configured maximum. Same pattern for the rate limiter buckets (capped at 10,000 entries).

## 7. Domain Knowledge: Payment Systems Reliability

### PCI-DSS alignment

This exercise touches several PCI-DSS requirements directly:

- **Requirement 3.6 (Cryptographic key management):** PCI-DSS requires documented procedures for key generation, distribution, storage, rotation, and destruction. Our `rotate-credentials.sh` script and the Vault KV v2 versioning satisfy the "documented rotation procedure" requirement. The Vault audit log satisfies the "audit trail of key access" requirement.

- **Requirement 10.2 (Audit trails):** PCI-DSS requires logging of all access to cardholder data environment components. Our structured audit logger (`app/audit.py`) captures every credential read, rotation event, and authentication failure with timestamps, actor identity (service name or request ID), and outcome. These logs are emitted as structured JSON, making them parseable by SIEM systems.

- **Requirement 6.5.3 (Insecure cryptographic storage):** Credentials are never stored in code, environment variables baked into images, or logs. They exist only in Vault (encrypted at rest in production) and in the application's memory (bounded cache with eviction).

### Transaction reliability math

Major payment gateways like Stripe and Adyen set SLA expectations of 99.95% uptime for merchant integrations. Let us do the math on what a "simple restart" costs:

- OrbitPay peak TPS: ~35 (based on 12-country e-commerce, conservative estimate)
- Rolling restart window with 2 replicas: ~30 seconds of single-replica mode, ~5 seconds of potential unavailability during readiness probe flip
- Failed transactions during 5-second window: 35 * 5 = 175 transactions
- Average transaction value (e-commerce): $45
- Revenue at risk per rotation event: 175 * $45 = $7,875
- If we rotate monthly: $94,500/year in potential failed transactions

Our zero-downtime approach eliminates this entirely. The CredentialManager polls every 30 seconds. During that 30-second window, the old credential (still valid) continues to work. Zero dropped transactions.

### Idempotency

Payment systems must be idempotent. If a client sends the same payment request twice (due to network timeout and retry), the system must process it exactly once. Our `PaymentRequest` model requires an `idempotency_key` field, and the GatewayClient passes it through to VaultGateway in every authorization request. VaultGateway (mock) uses this key to deduplicate. In a real system, we would also track idempotency keys in the payment service itself (Redis or in-memory with TTL) to avoid even hitting the gateway on duplicates.

### Circuit breaker tuning

The circuit breaker opens after 5 consecutive failures with a 30-second recovery window. These numbers come from payment industry norms:

- 5 failures (not 3): Payment gateways occasionally return 5xx during deployments. A threshold of 3 would cause false opens. 5 gives enough signal that the gateway is genuinely down.
- 30-second half-open timeout: Payment gateway outages typically last 30 seconds to 5 minutes. Probing every 30 seconds balances fast recovery against hammering a failing gateway.

## 8. Production Readiness Gaps

What would need to change to take this from a local demo to a production system processing real payments:

1. **Vault HA cluster:** 3+ nodes with Raft integrated storage (or Consul backend), TLS on all Vault ports, auto-unseal via AWS KMS or GCP Cloud KMS. Vault Operator for Kubernetes to manage the lifecycle. Estimated infra cost: $600/month for 3 nodes on r5.large.

2. **Authentication method:** Replace token auth with Kubernetes ServiceAccount auth. The pod's JWT is exchanged for a short-lived, scoped Vault token. No static tokens stored in K8s Secrets. This also means re-enabling `automountServiceAccountToken: true` for the payment service pod (with a dedicated ServiceAccount that has no RBAC privileges in K8s itself).

3. **Dynamic secrets (stretch):** Instead of static API keys stored in KV v2, Vault could generate short-lived credentials via a custom secrets engine. Each pod gets a unique credential that auto-expires. Leaked credentials are time-limited. This requires VaultGateway to support Vault as a credential authority -- unlikely for a third-party gateway, but ideal for internal services.

4. **Monitoring and alerting:** Real PagerDuty or OpsGenie integration for AlertManager. SLO-based alerting (e.g., "credential rotation success rate < 99.9% over 30 days" instead of threshold alerts). Grafana Cloud or a self-hosted Thanos for long-term metric retention (local Prometheus retains only 15 days by default).

5. **Network security:** Service mesh (Istio or Linkerd) for automatic mTLS between all pods. cert-manager for TLS certificate lifecycle. EncryptionConfiguration for etcd at-rest encryption.

6. **CI/CD hardening:** Image signing with cosign, SBOM generation with syft, vulnerability scanning with Trivy (blocking on CRITICAL CVEs), image policy enforcement with Kyverno. GitHub Actions pinned to SHA hashes (not just version tags) for supply chain security.

7. **Multi-region:** Active-active deployment across 2+ regions with regional Vault clusters replicating via Vault Enterprise Performance Replication (or separate Vault clusters with synchronized secrets via a control plane). Regional K8s clusters with federated service mesh.

8. **Compliance:** PodSecurityAdmission `enforce: restricted` (already implemented). EncryptionConfiguration for etcd (not implemented). Regular credential rotation on a schedule (not just manual/incident-driven). Audit log shipping to a SIEM (Splunk, Elastic, Datadog Logs) with retention per PCI-DSS Requirement 10.7 (at least 1 year, 3 months immediately available).

## 9. Cost Analysis

| Component | Dev/Demo (this exercise) | Production (monthly estimate) |
|-----------|--------------------------|-------------------------------|
| Vault | Free (OSS, dev mode, single pod) | ~$600 (3-node HA on r5.large + EBS) |
| Kubernetes | Free (kind on local machine) | ~$150-300 (EKS/GKE per cluster, plus node costs) |
| Prometheus + Grafana | Free (self-hosted in cluster) | ~$200 (Grafana Cloud Pro) or $0 (self-hosted + ops cost ~$100 in engineer time) |
| AlertManager integrations | N/A (webhook placeholders) | ~$25/user/month (PagerDuty Business) |
| Container registry | N/A (local images) | ~$10/month (ECR or GCR for 5-10 images) |
| cert-manager + TLS | N/A (no TLS in dev) | ~$0 (cert-manager is free, Let's Encrypt certs are free) |
| **Total** | **$0** | **~$985-1,135/month** |

**Alternative architecture cost comparison:**

AWS-native stack: Secrets Manager ($20/mo) + ECS Fargate ($100/mo for 2 tasks) + CloudWatch + Alarms ($50/mo) + ALB ($25/mo) = ~$195/month. Significantly cheaper, but: no audit trail as rich as Vault's, no KV v2 versioning (Secrets Manager has versioning but less ergonomic), vendor lock-in on all components, and CloudWatch alerting is less flexible than AlertManager routing.

The Vault-on-K8s approach costs 5x more in infrastructure but provides: portable infrastructure (runs on any cloud or on-prem), fine-grained audit trail, secret versioning with rollback, and a path to dynamic secrets. For a payment system where PCI-DSS compliance is mandatory, the additional cost is justified.

## 10. Architecture Invariants

These are non-negotiable constraints that must hold true across all future changes:

1. **Secrets never touch disk unencrypted.** Vault stores secrets in memory (dev mode) or encrypted at rest (production). The payment service holds secrets in-memory only. No `/tmp` files, no environment variable dumps.

2. **Fail closed, always.** If the service cannot verify credentials, it returns 503. It does not process payments with empty or default credentials. The `has_valid_credentials` property gates the readiness probe.

3. **Bounded everything.** Every in-memory data structure has a maximum size with eviction policy. Credential cache: 5 entries. Rate limiter buckets: 10,000. No unbounded growth.

4. **Audit every access.** Every credential read, rotation, and authentication failure produces a structured JSON audit event. No silent failures.

5. **Least privilege.** The payment service can only read its own secrets path. It cannot create, update, or delete secrets. It cannot access other services' paths. NetworkPolicies enforce network-level least privilege.
