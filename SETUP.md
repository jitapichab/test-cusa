# OrbitPay Secrets Infrastructure -- Setup Guide

## Prerequisites

Before you begin, ensure the following tools are installed:

| Tool | Minimum Version | Check Command | Install |
|------|----------------|---------------|---------|
| Docker Desktop | 24.0+ | `docker version` | [docker.com/get-docker](https://docs.docker.com/get-docker/) |
| kind | 0.20+ | `kind version` | `brew install kind` or [kind.sigs.k8s.io](https://kind.sigs.k8s.io/docs/user/quick-start/) |
| kubectl | 1.28+ | `kubectl version --client` | `brew install kubectl` or [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/) |
| bash | 4.0+ | `bash --version` | Pre-installed on macOS (via Homebrew if needed) and Linux |
| Python | 3.12+ | `python3 --version` | For running tests locally (optional) |

**Resource requirements:**
- RAM: ~4 GB free (kind cluster + Vault + services + monitoring)
- Disk: ~10 GB free (Docker images, kind node image)
- CPU: 2+ cores recommended
- Network: Internet access for pulling base images on first run

## Quick Start (1 command)

If the setup script is available:

```bash
./scripts/setup.sh
```

This script will create the kind cluster, build all Docker images, load them into kind, deploy all manifests, and run a basic smoke test. Estimated time: 3-5 minutes depending on your machine and whether Docker images are cached.

## Manual Steps

If you prefer to set up each component individually, or if the setup script encounters issues, follow these steps in order.

### Step 1: Create the kind cluster

```bash
kind create cluster --name orbitpay --config=- <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
```

Verify the cluster is running:

```bash
kubectl cluster-info --context kind-orbitpay
kubectl get nodes
```

You should see 1 control-plane and 2 worker nodes in `Ready` state.

### Step 2: Build Docker images

```bash
docker build -t payment-gateway-connector:latest services/payment-gateway-connector/
docker build -t vaultgateway-mock:latest services/vaultgateway-mock/
```

### Step 3: Load images into kind

kind clusters cannot pull from your local Docker daemon directly. You must load images explicitly:

```bash
kind load docker-image payment-gateway-connector:latest --name orbitpay
kind load docker-image vaultgateway-mock:latest --name orbitpay
```

### Step 4: Create the namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

Verify:

```bash
kubectl get namespace orbitpay
```

The namespace should have labels `pod-security.kubernetes.io/enforce: restricted`.

### Step 5: Deploy Vault

```bash
kubectl apply -f k8s/vault/secret.yaml
kubectl apply -f k8s/vault/configmap.yaml
kubectl apply -f k8s/vault/service.yaml
kubectl apply -f k8s/vault/networkpolicy.yaml
kubectl apply -f k8s/vault/statefulset.yaml
```

Wait for the Vault pod to be ready:

```bash
kubectl -n orbitpay wait --for=condition=ready pod/vault-0 --timeout=120s
```

### Step 6: Initialize Vault (policies, secrets, audit)

```bash
kubectl apply -f k8s/vault/init-job.yaml
```

Wait for the init job to complete:

```bash
kubectl -n orbitpay wait --for=condition=complete job/vault-init --timeout=120s
```

Check the init job logs to verify it succeeded:

```bash
kubectl -n orbitpay logs job/vault-init
```

You should see messages confirming KV v2 engine enabled, policy written, initial credentials stored, and audit log enabled.

### Step 7: Deploy VaultGateway mock

```bash
kubectl apply -f k8s/vaultgateway-mock/
```

Wait for the mock gateway to be ready:

```bash
kubectl -n orbitpay wait --for=condition=available deployment/vaultgateway-mock --timeout=120s
```

### Step 8: Deploy payment-gateway-connector

```bash
kubectl apply -f k8s/payment-gateway-connector/
```

Wait for the deployment to be ready:

```bash
kubectl -n orbitpay wait --for=condition=available deployment/payment-gateway-connector --timeout=120s
```

### Step 9: Deploy Prometheus

```bash
kubectl apply -f k8s/monitoring/prometheus/
```

Wait for Prometheus to be ready:

```bash
kubectl -n orbitpay wait --for=condition=available deployment/prometheus --timeout=120s 2>/dev/null || \
kubectl -n orbitpay wait --for=condition=ready pod -l app.kubernetes.io/name=prometheus --timeout=120s
```

### Step 10: Deploy AlertManager

```bash
kubectl apply -f k8s/monitoring/alertmanager/
```

### Step 11: Deploy Grafana

```bash
kubectl apply -f k8s/monitoring/grafana/
```

### Step 12: Verify all pods are running

```bash
kubectl -n orbitpay get pods -o wide
```

Expected output (names will vary):

```
NAME                                          READY   STATUS      RESTARTS   AGE
vault-0                                       1/1     Running     0          2m
vault-init-xxxxx                              0/1     Completed   0          1m
vaultgateway-mock-xxxxxxxxx-xxxxx             1/1     Running     0          1m
payment-gateway-connector-xxxxxxxxx-xxxxx     1/1     Running     0          1m
payment-gateway-connector-xxxxxxxxx-yyyyy     1/1     Running     0          1m
prometheus-xxxxxxxxx-xxxxx                    1/1     Running     0          45s
alertmanager-xxxxxxxxx-xxxxx                  1/1     Running     0          30s
grafana-xxxxxxxxx-xxxxx                       1/1     Running     0          30s
```

All pods should be in `Running` status (except `vault-init` which should be `Completed`).

### Step 13: Port-forward services

Open separate terminal windows (or use `&` to background):

```bash
# Payment service API (terminal 1)
kubectl -n orbitpay port-forward svc/payment-gateway-connector 8000:8000 &

# Vault UI (terminal 2)
kubectl -n orbitpay port-forward svc/vault 8200:8200 &

# Prometheus UI (terminal 3)
kubectl -n orbitpay port-forward svc/prometheus 9090:9090 &

# Grafana UI (terminal 4)
kubectl -n orbitpay port-forward svc/grafana 3000:3000 &
```

### Step 14: Test payment authorization

Send a test payment request:

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
  }' | python3 -m json.tool
```

Expected response:

```json
{
    "transaction_id": "txn_...",
    "status": "approved",
    "merchant_id": "merchant-001",
    "amount": 99.99,
    "currency": "USD",
    "timestamp": "2026-02-25T..."
}
```

Check the health endpoint:

```bash
curl -s http://localhost:8000/health/ready | python3 -m json.tool
```

You should see `vault_connected: true` and `credential_status: "valid"`.

### Step 15: Run credential rotation demo

```bash
./scripts/rotate-credentials.sh
```

This script will:
1. Write a new credential to Vault (creating a new KV v2 version)
2. Wait for the payment service to detect the change (~30 seconds)
3. Send a test payment to verify the new credential works
4. Show the Prometheus metric `credential_rotation_total` incremented

## Simulating Credential Rotation (Manual)

If you want to perform rotation manually step by step:

### Step 1: Check current credential version

```bash
kubectl -n orbitpay exec vault-0 -- vault kv get secret/orbitpay/vaultgateway
```

Note the `version` number (should be 1 after initial setup).

### Step 2: Write a new credential

```bash
kubectl -n orbitpay exec vault-0 -- vault kv put secret/orbitpay/vaultgateway \
  api_key="ogw-rotated-key-$(date +%s)" \
  api_secret="ogw-rotated-secret-$(date +%s)" \
  merchant_id="orbitpay-merchant-001" \
  gateway_url="http://vaultgateway-mock.orbitpay.svc.cluster.local:8080" \
  created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  version="2"
```

### Step 3: Wait for detection

The CredentialManager polls every 30 seconds. Wait up to 30 seconds, then check:

```bash
curl -s http://localhost:8000/health/ready | python3 -m json.tool
```

The `credential_status` should still be `"valid"` and you may see the version updated.

### Step 4: Verify rotation metrics

```bash
curl -s http://localhost:8000/metrics | grep credential_rotation_total
```

The counter should show `1.0` (one rotation detected).

### Step 5: Verify payment still works

```bash
curl -s -X POST http://localhost:8000/api/v1/payments/authorize \
  -H "Content-Type: application/json" \
  -H "X-API-Key: default-api-key" \
  -d '{
    "merchant_id": "merchant-002",
    "amount": 49.95,
    "currency": "EUR",
    "card_token": "tok_test_mastercard_5555",
    "idempotency_key": "idem-rotation-test-001"
  }' | python3 -m json.tool
```

The payment should succeed with the new credential. No restart was needed.

## Troubleshooting

### Pods in CrashLoopBackOff

**Symptoms:** `kubectl -n orbitpay get pods` shows a pod with `STATUS: CrashLoopBackOff` and increasing `RESTARTS`.

**Diagnosis:**

```bash
# Check pod logs
kubectl -n orbitpay logs <pod-name> --previous

# Check events
kubectl -n orbitpay describe pod <pod-name>
```

**Common causes:**
- Payment service cannot reach Vault: Check that `vault-0` is `Running` and the `vault` Service exists. Verify the `VAULT_ADDR` in the ConfigMap matches the Service DNS name (`http://vault.orbitpay.svc.cluster.local:8200`).
- Vault secrets do not exist: The `vault-init` Job may not have completed. Re-run `kubectl apply -f k8s/vault/init-job.yaml` and wait for completion.
- Missing environment variables: Check that ConfigMap and Secret are correctly applied (`kubectl -n orbitpay get configmap,secret`).

### Vault not ready

**Symptoms:** `vault-0` pod is `Running` but not `Ready` (0/1).

**Diagnosis:**

```bash
kubectl -n orbitpay logs vault-0
kubectl -n orbitpay exec vault-0 -- vault status
```

**Common causes:**
- Insufficient resources: kind nodes may not have enough memory. Increase Docker Desktop memory allocation to 6 GB.
- The readiness probe runs `vault status`. In dev mode, this should succeed immediately. If it fails, check that the `VAULT_DEV_ROOT_TOKEN_ID` Secret exists and is base64-encoded correctly.

### ImagePullBackOff in kind

**Symptoms:** Pod shows `ErrImagePull` or `ImagePullBackOff`.

**Diagnosis:**

```bash
kubectl -n orbitpay describe pod <pod-name> | grep -A5 "Events"
```

**Fix:** kind cannot pull from your local Docker daemon. You must load images explicitly:

```bash
# Verify images exist locally
docker images | grep -E 'payment-gateway-connector|vaultgateway-mock'

# Load into kind
kind load docker-image payment-gateway-connector:latest --name orbitpay
kind load docker-image vaultgateway-mock:latest --name orbitpay

# Verify images are in the kind node
docker exec -it orbitpay-worker crictl images | grep -E 'payment|vault'
```

### NetworkPolicy blocking traffic

**Symptoms:** Service returns 503 or times out. Pod logs show connection refused or timeout errors to Vault or VaultGateway.

**Diagnosis:**

```bash
# Check if pods have correct labels
kubectl -n orbitpay get pods --show-labels

# Verify NetworkPolicies
kubectl -n orbitpay get networkpolicy -o yaml

# Test connectivity from inside a pod
kubectl -n orbitpay exec -it deployment/payment-gateway-connector -- \
  wget -qO- --timeout=5 http://vault.orbitpay.svc.cluster.local:8200/v1/sys/health || echo "BLOCKED"
```

**Common causes:**
- Label mismatch: NetworkPolicies select pods by label. If a pod's labels do not match the ingress/egress rules, traffic is denied. Verify that `app.kubernetes.io/name` labels match across Deployment specs and NetworkPolicy selectors.
- kind CNI: The default kindnet CNI supports NetworkPolicies. If you're using an older kind version, NetworkPolicies may not be enforced. Upgrade kind to 0.20+.

### Port-forward not working

**Symptoms:** `kubectl port-forward` exits immediately or connection refused on localhost.

**Diagnosis:**

```bash
# Check if the service exists and has endpoints
kubectl -n orbitpay get svc
kubectl -n orbitpay get endpoints

# Check if the target pod is running
kubectl -n orbitpay get pods -l app.kubernetes.io/name=payment-gateway-connector
```

**Fix:** Ensure the pod is in `Running` and `Ready` state. The Service must have endpoints. If endpoints are empty, the Service selector does not match any pod labels.

### Prometheus not scraping targets

**Symptoms:** Grafana dashboards are empty. Prometheus targets page (`http://localhost:9090/targets`) shows targets as DOWN.

**Diagnosis:**

```bash
# Check Prometheus config
kubectl -n orbitpay get configmap prometheus-config -o yaml

# Check if Prometheus can reach targets
kubectl -n orbitpay exec -it deployment/prometheus -- \
  wget -qO- --timeout=5 http://payment-gateway-connector.orbitpay.svc.cluster.local:8000/metrics || echo "BLOCKED"
```

**Common causes:** NetworkPolicy for payment-gateway-connector must allow ingress from Prometheus on port 8000. Check that the Prometheus pod has the label `app.kubernetes.io/name: prometheus`.

## Teardown

To destroy the entire cluster and clean up:

```bash
./scripts/teardown.sh
```

Or manually:

```bash
# Delete the kind cluster (removes all resources)
kind delete cluster --name orbitpay

# Optionally remove Docker images
docker rmi payment-gateway-connector:latest vaultgateway-mock:latest 2>/dev/null
```

## Accessing UIs

Once port-forwarding is active:

| Service | URL | Credentials |
|---------|-----|-------------|
| Payment API | http://localhost:8000 | Header: `X-API-Key: default-api-key` |
| Health Check | http://localhost:8000/health/ready | None |
| Metrics | http://localhost:8000/metrics | None |
| Vault UI | http://localhost:8200 | Token: `dev-root-token-orbitpay` |
| Prometheus | http://localhost:9090 | None |
| Grafana | http://localhost:3000 | admin / admin (change on first login) |
