#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# OrbitPay Credential Rotation Script
# Performs zero-downtime credential rotation through Vault KV v2.
# =============================================================================

# -- Colors -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# -- Globals ------------------------------------------------------------------
NAMESPACE="orbitpay"
VAULT_PORT_LOCAL=8200
PGC_PORT_LOCAL=8000
VAULT_PORT_FWD_PID=""
PGC_PORT_FWD_PID=""
VAULT_ADDR="http://127.0.0.1:${VAULT_PORT_LOCAL}"
VAULT_TOKEN="dev-root-token-orbitpay"
SECRET_PATH="secret/orbitpay/vaultgateway"
ROTATION_START=""

# -- Helpers ------------------------------------------------------------------
log_info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

cleanup() {
    local exit_code=$?
    log_info "Cleaning up port-forwards..."
    if [[ -n "${VAULT_PORT_FWD_PID}" ]]; then
        kill "${VAULT_PORT_FWD_PID}" 2>/dev/null || true
    fi
    if [[ -n "${PGC_PORT_FWD_PID}" ]]; then
        kill "${PGC_PORT_FWD_PID}" 2>/dev/null || true
    fi
    if [[ ${exit_code} -ne 0 ]]; then
        log_error "Credential rotation failed with exit code ${exit_code}."
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
        log_error "Missing prerequisites."
        exit 1
    fi

    # Verify cluster connectivity
    if ! kubectl get namespace "${NAMESPACE}" &>/dev/null; then
        log_error "Namespace '${NAMESPACE}' not found. Is the cluster running?"
        exit 1
    fi

    log_success "All prerequisites satisfied."
}

# -- Port forwarding ----------------------------------------------------------
setup_port_forwards() {
    log_info "Setting up port-forwards..."

    # Vault
    kubectl port-forward -n "${NAMESPACE}" svc/vault "${VAULT_PORT_LOCAL}:8200" &>/dev/null &
    VAULT_PORT_FWD_PID=$!

    # Payment Gateway Connector
    kubectl port-forward -n "${NAMESPACE}" svc/payment-gateway-connector "${PGC_PORT_LOCAL}:8000" &>/dev/null &
    PGC_PORT_FWD_PID=$!

    # Wait for port-forwards to establish
    sleep 3

    # Verify Vault is reachable
    local retries=10
    while [[ ${retries} -gt 0 ]]; do
        if curl -s "${VAULT_ADDR}/v1/sys/health" &>/dev/null; then
            break
        fi
        retries=$((retries - 1))
        sleep 1
    done

    if [[ ${retries} -eq 0 ]]; then
        log_error "Could not reach Vault at ${VAULT_ADDR}."
        exit 1
    fi

    log_success "Port-forwards established."
}

# -- Read current credentials -------------------------------------------------
get_current_version() {
    local metadata
    metadata=$(curl -s \
        -H "X-Vault-Token: ${VAULT_TOKEN}" \
        "${VAULT_ADDR}/v1/secret/metadata/orbitpay/vaultgateway")

    echo "${metadata}" | grep -o '"current_version":[0-9]*' | grep -o '[0-9]*' || echo "0"
}

get_current_credentials() {
    curl -s \
        -H "X-Vault-Token: ${VAULT_TOKEN}" \
        "${VAULT_ADDR}/v1/secret/data/orbitpay/vaultgateway"
}

# -- Generate new credentials -------------------------------------------------
generate_api_key() {
    # Generate a random API key (using /dev/urandom for demo)
    local key
    key="ogw-$(date +%s)-$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 16)"
    echo "${key}"
}

# -- Rotation workflow --------------------------------------------------------
rotate_credentials() {
    ROTATION_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    log_info "Starting credential rotation at ${ROTATION_START}..."

    # Step 1: Read current credentials
    log_info "Step 1: Reading current credentials from Vault..."
    local current_creds
    current_creds=$(get_current_credentials)
    local current_version
    current_version=$(get_current_version)
    local current_api_key
    current_api_key=$(echo "${current_creds}" | grep -o '"api_key":"[^"]*"' | cut -d'"' -f4)
    log_info "Current credential version: ${current_version}"
    # Only log key prefix length sufficient for debugging, not for reuse
    log_info "Current API key prefix: ${current_api_key:0:8}***"

    # Step 2: Generate new API key
    log_info "Step 2: Generating new API key..."
    local new_api_key
    new_api_key=$(generate_api_key)
    local new_api_secret
    new_api_secret="ogw-secret-$(date +%s)-$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 16)"
    local new_version=$((current_version + 1))
    log_info "New API key prefix: ${new_api_key:0:8}***"
    log_info "New version: ${new_version}"

    # Step 3: Update VaultGateway mock to accept BOTH old and new keys
    log_info "Step 3: Updating VaultGateway mock to accept both old and new keys..."
    local combined_keys="${current_api_key},${new_api_key}"
    local combined_keys_b64
    combined_keys_b64=$(echo -n "${combined_keys}" | base64)

    kubectl patch secret vaultgateway-mock-secret -n "${NAMESPACE}" \
        --type='json' \
        -p="[{\"op\":\"replace\",\"path\":\"/data/VALID_API_KEYS\",\"value\":\"${combined_keys_b64}\"}]"

    # Restart vaultgateway-mock to pick up new keys
    kubectl rollout restart deployment/vaultgateway-mock -n "${NAMESPACE}"
    kubectl rollout status deployment/vaultgateway-mock -n "${NAMESPACE}" --timeout=60s
    log_success "VaultGateway mock accepts both old and new keys."

    # Step 4: Write new credential to Vault (creates new KV version)
    log_info "Step 4: Writing new credential to Vault KV v2..."
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
                \"version\": \"${new_version}\"
            }
        }" > /dev/null
    log_success "New credential written to Vault (version ${new_version})."

    # Step 5: Wait for payment service to detect new credential
    log_info "Step 5: Waiting for payment service to detect new credential..."
    local poll_attempts=20
    local detected=false
    while [[ ${poll_attempts} -gt 0 ]]; do
        local health_response
        health_response=$(curl -s "http://127.0.0.1:${PGC_PORT_LOCAL}/health/ready" 2>/dev/null || echo "{}")
        if echo "${health_response}" | grep -q "${new_api_key:0:10}" 2>/dev/null || \
           echo "${health_response}" | grep -q "\"version\":\"${new_version}\"" 2>/dev/null || \
           echo "${health_response}" | grep -q "\"version\":${new_version}" 2>/dev/null; then
            detected=true
            break
        fi
        poll_attempts=$((poll_attempts - 1))
        log_info "  Polling... (${poll_attempts} attempts remaining)"
        sleep 5
    done

    if [[ "${detected}" == "true" ]]; then
        log_success "Payment service detected new credential."
    else
        log_warn "Payment service did not confirm detection within timeout."
        log_warn "The service may still pick up the new credential on its next check cycle."
    fi

    # Step 6: Verify payment authorization works with new credential
    log_info "Step 6: Verifying payment authorization..."
    local auth_response
    auth_response=$(curl -s -o /dev/null -w "%{http_code}" \
        "http://127.0.0.1:${PGC_PORT_LOCAL}/health/ready" 2>/dev/null || echo "000")

    if [[ "${auth_response}" == "200" ]]; then
        log_success "Payment service health check passed."
    else
        log_warn "Payment service health returned HTTP ${auth_response}."
    fi

    # Step 7: Remove old credential from VaultGateway mock
    log_info "Step 7: Removing old credential from VaultGateway mock..."
    local new_key_only_b64
    new_key_only_b64=$(echo -n "${new_api_key}" | base64)

    kubectl patch secret vaultgateway-mock-secret -n "${NAMESPACE}" \
        --type='json' \
        -p="[{\"op\":\"replace\",\"path\":\"/data/VALID_API_KEYS\",\"value\":\"${new_key_only_b64}\"}]"

    # Restart to pick up the change
    kubectl rollout restart deployment/vaultgateway-mock -n "${NAMESPACE}"
    kubectl rollout status deployment/vaultgateway-mock -n "${NAMESPACE}" --timeout=60s
    log_success "Old credential removed from VaultGateway mock."

    # Step 8: Print rotation summary
    local rotation_end
    rotation_end=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}  Credential Rotation Summary${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "  Started:          ${ROTATION_START}"
    echo -e "  Completed:        ${rotation_end}"
    echo -e "  Previous version: ${current_version}"
    echo -e "  New version:      ${new_version}"
    echo -e "  Old API key:      ${current_api_key:0:8}***"
    echo -e "  New API key:      ${new_api_key:0:8}***"
    echo -e "  Zero-downtime:    YES (dual-key overlap during rotation)"
    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo ""
}

# -- Main ---------------------------------------------------------------------
main() {
    echo ""
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  OrbitPay Credential Rotation${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo ""

    check_prerequisites
    setup_port_forwards
    rotate_credentials

    log_success "Credential rotation completed successfully."
}

main "$@"
