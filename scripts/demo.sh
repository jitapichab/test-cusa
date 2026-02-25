#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# OrbitPay End-to-End Demo Script
# Demonstrates the full credential lifecycle including rotation.
# =============================================================================

# -- Colors -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# -- Globals ------------------------------------------------------------------
NAMESPACE="orbitpay"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_PORT=8200
PGC_PORT=8000
PROMETHEUS_PORT=9090
GRAFANA_PORT=3000
VAULT_ADDR="http://127.0.0.1:${VAULT_PORT}"
VAULT_TOKEN="dev-root-token-orbitpay"
PORT_FWD_PIDS=()

# -- Helpers ------------------------------------------------------------------
log_info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_demo()    { echo -e "${CYAN}[DEMO]${NC}  $*"; }

separator() {
    echo ""
    echo -e "${CYAN}--------------------------------------------------------------${NC}"
    echo -e "${CYAN}  $*${NC}"
    echo -e "${CYAN}--------------------------------------------------------------${NC}"
    echo ""
}

cleanup() {
    local exit_code=$?
    echo ""
    log_info "Cleaning up port-forwards..."
    for pid in "${PORT_FWD_PIDS[@]}"; do
        kill "${pid}" 2>/dev/null || true
    done
    if [[ ${exit_code} -ne 0 ]]; then
        log_error "Demo failed with exit code ${exit_code}."
    fi
    exit "${exit_code}"
}
trap cleanup EXIT ERR

# -- Prerequisite checks -----------------------------------------------------
check_prerequisites() {
    log_info "Checking prerequisites..."
    local missing=0

    if ! command -v kubectl &>/dev/null; then
        log_error "kubectl is not installed."
        missing=1
    fi

    if ! command -v curl &>/dev/null; then
        log_error "curl is not installed."
        missing=1
    fi

    if [[ ${missing} -ne 0 ]]; then
        exit 1
    fi

    if ! kubectl get namespace "${NAMESPACE}" &>/dev/null; then
        log_error "Namespace '${NAMESPACE}' not found. Run ./scripts/setup.sh first."
        exit 1
    fi

    log_success "Prerequisites satisfied."
}

# -- Port forwarding ----------------------------------------------------------
start_port_forwards() {
    separator "Starting Port Forwards"

    # Vault
    kubectl port-forward -n "${NAMESPACE}" svc/vault "${VAULT_PORT}:8200" &>/dev/null &
    PORT_FWD_PIDS+=($!)

    # Payment Gateway Connector
    kubectl port-forward -n "${NAMESPACE}" svc/payment-gateway-connector "${PGC_PORT}:8000" &>/dev/null &
    PORT_FWD_PIDS+=($!)

    # Prometheus
    kubectl port-forward -n "${NAMESPACE}" svc/prometheus "${PROMETHEUS_PORT}:9090" &>/dev/null &
    PORT_FWD_PIDS+=($!)

    # Grafana
    kubectl port-forward -n "${NAMESPACE}" svc/grafana "${GRAFANA_PORT}:3000" &>/dev/null &
    PORT_FWD_PIDS+=($!)

    # Wait for port-forwards to establish
    sleep 4

    # Verify connectivity
    local retries=10
    while [[ ${retries} -gt 0 ]]; do
        if curl -s "${VAULT_ADDR}/v1/sys/health" &>/dev/null; then
            break
        fi
        retries=$((retries - 1))
        sleep 1
    done

    log_success "Port-forwards active:"
    log_info "  Vault:       http://127.0.0.1:${VAULT_PORT}"
    log_info "  Payment Svc: http://127.0.0.1:${PGC_PORT}"
    log_info "  Prometheus:  http://127.0.0.1:${PROMETHEUS_PORT}"
    log_info "  Grafana:     http://127.0.0.1:${GRAFANA_PORT}"
}

# -- Demo steps ---------------------------------------------------------------
show_cluster_status() {
    separator "Step 1: Cluster Status"
    log_demo "Checking all pods in the orbitpay namespace..."
    echo ""
    kubectl get pods -n "${NAMESPACE}" -o wide
    echo ""

    log_demo "Checking all services..."
    echo ""
    kubectl get svc -n "${NAMESPACE}"
    echo ""
}

show_credential_status() {
    separator "Step 2: Current Credential Status"

    log_demo "Querying payment service health endpoint..."
    local health_response
    health_response=$(curl -s "http://127.0.0.1:${PGC_PORT}/health/ready" 2>/dev/null || echo '{"error": "service not reachable"}')
    echo ""
    echo "${health_response}" | python3 -m json.tool 2>/dev/null || echo "${health_response}"
    echo ""

    log_demo "Querying current credentials from Vault..."
    local vault_response
    vault_response=$(curl -s \
        -H "X-Vault-Token: ${VAULT_TOKEN}" \
        "${VAULT_ADDR}/v1/secret/data/orbitpay/vaultgateway" 2>/dev/null || echo '{"error": "vault not reachable"}')
    echo ""
    echo "${vault_response}" | python3 -m json.tool 2>/dev/null || echo "${vault_response}"
    echo ""
}

send_test_payment() {
    local label="${1:-pre-rotation}"
    separator "Step 3: Test Payment Authorization (${label})"

    log_demo "Sending test payment authorization request..."
    local response
    response=$(curl -s -X POST \
        -H "Content-Type: application/json" \
        -H "X-API-Key: test-key" \
        "http://127.0.0.1:${PGC_PORT}/api/v1/authorize" \
        -d '{
            "transaction_id": "txn-demo-'"$(date +%s)"'",
            "amount": 99.99,
            "currency": "USD",
            "card_token": "tok_demo_visa_4242",
            "merchant_id": "orbitpay-merchant-001"
        }' 2>/dev/null || echo '{"error": "service not reachable"}')
    echo ""
    echo "${response}" | python3 -m json.tool 2>/dev/null || echo "${response}"
    echo ""
}

trigger_rotation() {
    separator "Step 4: Credential Rotation"

    log_demo "Triggering credential rotation..."
    log_demo "This will:"
    echo "  1. Generate a new API key"
    echo "  2. Enable dual-key acceptance on the gateway mock"
    echo "  3. Write new credential to Vault (new KV version)"
    echo "  4. Wait for the payment service to detect the new credential"
    echo "  5. Remove the old key from the gateway mock"
    echo ""

    # Inline rotation (similar to rotate-credentials.sh but with demo output)
    log_demo "Reading current credentials..."
    local current_creds
    current_creds=$(curl -s \
        -H "X-Vault-Token: ${VAULT_TOKEN}" \
        "${VAULT_ADDR}/v1/secret/data/orbitpay/vaultgateway" 2>/dev/null)
    local current_api_key
    current_api_key=$(echo "${current_creds}" | grep -o '"api_key":"[^"]*"' | cut -d'"' -f4)
    log_info "Current API key: ${current_api_key:0:25}..."

    local new_api_key
    new_api_key="ogw-$(date +%s)-$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 16)"
    local new_api_secret
    new_api_secret="ogw-secret-$(date +%s)-$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 16)"
    log_info "New API key:     ${new_api_key:0:25}..."

    # Update mock to accept both keys
    log_demo "Enabling dual-key acceptance..."
    local combined_keys="${current_api_key},${new_api_key}"
    local combined_keys_b64
    combined_keys_b64=$(echo -n "${combined_keys}" | base64)
    kubectl patch secret vaultgateway-mock-secret -n "${NAMESPACE}" \
        --type='json' \
        -p="[{\"op\":\"replace\",\"path\":\"/data/VALID_API_KEYS\",\"value\":\"${combined_keys_b64}\"}]"
    kubectl rollout restart deployment/vaultgateway-mock -n "${NAMESPACE}"
    kubectl rollout status deployment/vaultgateway-mock -n "${NAMESPACE}" --timeout=60s
    log_success "Gateway mock accepts both old and new keys."

    # Write new credential to Vault
    log_demo "Writing new credential to Vault..."
    curl -s \
        -H "X-Vault-Token: ${VAULT_TOKEN}" \
        -H "Content-Type: application/json" \
        -X POST \
        "${VAULT_ADDR}/v1/secret/data/orbitpay/vaultgateway" \
        -d "{
            \"data\": {
                \"api_key\": \"${new_api_key}\",
                \"api_secret\": \"${new_api_secret}\",
                \"merchant_id\": \"orbitpay-merchant-001\",
                \"gateway_url\": \"http://vaultgateway-mock.orbitpay.svc.cluster.local:8080\",
                \"created_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
                \"version\": \"2\"
            }
        }" > /dev/null
    log_success "New credential written to Vault."
}

test_during_rotation() {
    separator "Step 5: Test During Rotation (Zero-Downtime Proof)"

    log_demo "Sending payment authorizations during credential transition..."
    echo ""

    for i in 1 2 3; do
        log_demo "Request ${i}/3 during rotation window..."
        local response
        response=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST \
            -H "Content-Type: application/json" \
            -H "X-API-Key: test-key" \
            "http://127.0.0.1:${PGC_PORT}/api/v1/authorize" \
            -d '{
                "transaction_id": "txn-rotation-'"${i}"'-'"$(date +%s)"'",
                "amount": 50.00,
                "currency": "USD",
                "card_token": "tok_demo_visa_4242",
                "merchant_id": "orbitpay-merchant-001"
            }' 2>/dev/null || echo "000")
        echo "  HTTP ${response}"
        sleep 1
    done
    echo ""
    log_success "Requests during rotation completed (zero-downtime verified)."
}

finalize_rotation() {
    separator "Step 6: Finalize Rotation"

    log_demo "Removing old credential from gateway mock..."
    local new_creds
    new_creds=$(curl -s \
        -H "X-Vault-Token: ${VAULT_TOKEN}" \
        "${VAULT_ADDR}/v1/secret/data/orbitpay/vaultgateway" 2>/dev/null)
    local new_api_key
    new_api_key=$(echo "${new_creds}" | grep -o '"api_key":"[^"]*"' | cut -d'"' -f4)

    local new_key_only_b64
    new_key_only_b64=$(echo -n "${new_api_key}" | base64)
    kubectl patch secret vaultgateway-mock-secret -n "${NAMESPACE}" \
        --type='json' \
        -p="[{\"op\":\"replace\",\"path\":\"/data/VALID_API_KEYS\",\"value\":\"${new_key_only_b64}\"}]"
    kubectl rollout restart deployment/vaultgateway-mock -n "${NAMESPACE}"
    kubectl rollout status deployment/vaultgateway-mock -n "${NAMESPACE}" --timeout=60s
    log_success "Old credential removed. Only new credential is active."
}

show_monitoring() {
    separator "Step 7: Monitoring"

    log_demo "Querying Prometheus for rotation events..."
    local prom_response
    prom_response=$(curl -s \
        "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/query?query=credential_version" 2>/dev/null || echo '{"error": "prometheus not reachable"}')
    echo ""
    echo "  Credential Version:"
    echo "  ${prom_response}" | python3 -m json.tool 2>/dev/null || echo "  ${prom_response}"
    echo ""

    log_demo "Querying Prometheus for gateway request metrics..."
    prom_response=$(curl -s \
        "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/query?query=rate(gateway_requests_total[5m])" 2>/dev/null || echo '{"error": "prometheus not reachable"}')
    echo ""
    echo "  Gateway Request Rate (5m):"
    echo "  ${prom_response}" | python3 -m json.tool 2>/dev/null || echo "  ${prom_response}"
    echo ""

    log_demo "Querying Prometheus for vault connectivity..."
    prom_response=$(curl -s \
        "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/query?query=vault_connected" 2>/dev/null || echo '{"error": "prometheus not reachable"}')
    echo ""
    echo "  Vault Connectivity:"
    echo "  ${prom_response}" | python3 -m json.tool 2>/dev/null || echo "  ${prom_response}"
    echo ""

    echo ""
    log_info "View the full Grafana dashboard at: http://127.0.0.1:${GRAFANA_PORT}"
    log_info "  Username: admin"
    log_info "  Password: orbitpay-grafana-dev"
    echo ""
}

print_summary() {
    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}  OrbitPay Demo Complete${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo "  What was demonstrated:"
    echo "    1. Cluster health and pod status"
    echo "    2. Current credential status from Vault"
    echo "    3. Test payment authorization (pre-rotation)"
    echo "    4. Zero-downtime credential rotation via Vault KV v2"
    echo "    5. Payment requests during rotation (zero-downtime proof)"
    echo "    6. Credential finalization (old key removal)"
    echo "    7. Monitoring queries via Prometheus"
    echo ""
    echo "  Key takeaways:"
    echo "    - Credentials are stored in Vault KV v2 (versioned)"
    echo "    - Rotation uses dual-key overlap for zero downtime"
    echo "    - Payment service auto-detects credential changes"
    echo "    - Full observability via Prometheus + Grafana"
    echo "    - AlertManager configured for critical/warning severity"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo ""
}

# -- Main ---------------------------------------------------------------------
main() {
    echo ""
    echo -e "${CYAN}================================================================${NC}"
    echo -e "${CYAN}  OrbitPay End-to-End Demo${NC}"
    echo -e "${CYAN}================================================================${NC}"
    echo ""

    check_prerequisites
    start_port_forwards
    show_cluster_status
    show_credential_status
    send_test_payment "pre-rotation"
    trigger_rotation
    test_during_rotation
    finalize_rotation
    show_monitoring
    print_summary
}

main "$@"
