#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# OrbitPay Infrastructure Setup Script
# Bootstrap a complete OrbitPay environment on a local kind cluster.
# =============================================================================

# -- Colors -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# -- Globals ------------------------------------------------------------------
CLUSTER_NAME="orbitpay"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
K8S_DIR="${PROJECT_DIR}/k8s"
SERVICES_DIR="${PROJECT_DIR}/services"

# -- Helpers ------------------------------------------------------------------
log_info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

cleanup() {
    local exit_code=$?
    if [[ ${exit_code} -ne 0 ]]; then
        log_error "Setup failed with exit code ${exit_code}."
        log_error "Check the output above for details."
        log_warn  "You can re-run this script -- it is idempotent."
    fi
    exit "${exit_code}"
}
trap cleanup ERR

# -- Prerequisite checks -----------------------------------------------------
check_prerequisites() {
    log_info "Checking prerequisites..."
    local missing=0

    if ! command -v docker &>/dev/null; then
        log_error "docker is not installed."
        echo "  Install: https://docs.docker.com/get-docker/"
        missing=1
    fi

    if ! command -v kind &>/dev/null; then
        log_error "kind is not installed."
        echo "  Install: https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
        missing=1
    fi

    if ! command -v kubectl &>/dev/null; then
        log_error "kubectl is not installed."
        echo "  Install: https://kubernetes.io/docs/tasks/tools/"
        missing=1
    fi

    if [[ ${missing} -ne 0 ]]; then
        log_error "Missing prerequisites. Install the tools above and try again."
        exit 1
    fi

    log_success "All prerequisites satisfied."
}

# -- Kind cluster -------------------------------------------------------------
create_cluster() {
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        log_warn "Kind cluster '${CLUSTER_NAME}' already exists. Skipping creation."
        return 0
    fi

    log_info "Creating kind cluster '${CLUSTER_NAME}' (1 control-plane, 2 workers)..."
    kind create cluster --name "${CLUSTER_NAME}" --config - <<'KINDCFG'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
KINDCFG
    log_success "Kind cluster '${CLUSTER_NAME}' created."
}

# -- Docker images ------------------------------------------------------------
build_and_load_images() {
    log_info "Building Docker images..."

    if [[ -d "${SERVICES_DIR}/payment-gateway-connector" ]]; then
        log_info "Building payment-gateway-connector image..."
        docker build -t payment-gateway-connector:latest "${SERVICES_DIR}/payment-gateway-connector"
        log_success "payment-gateway-connector image built."
    else
        log_warn "Directory ${SERVICES_DIR}/payment-gateway-connector not found. Skipping build."
    fi

    if [[ -d "${SERVICES_DIR}/vaultgateway-mock" ]]; then
        log_info "Building vaultgateway-mock image..."
        docker build -t vaultgateway-mock:latest "${SERVICES_DIR}/vaultgateway-mock"
        log_success "vaultgateway-mock image built."
    else
        log_warn "Directory ${SERVICES_DIR}/vaultgateway-mock not found. Skipping build."
    fi

    log_info "Loading images into kind cluster..."
    kind load docker-image payment-gateway-connector:latest --name "${CLUSTER_NAME}" 2>/dev/null || \
        log_warn "Could not load payment-gateway-connector image (may not exist yet)."
    kind load docker-image vaultgateway-mock:latest --name "${CLUSTER_NAME}" 2>/dev/null || \
        log_warn "Could not load vaultgateway-mock image (may not exist yet)."

    log_success "Docker images processed."
}

# -- Wait helpers -------------------------------------------------------------
wait_for_rollout() {
    local resource="$1"
    local namespace="$2"
    local timeout="${3:-120s}"
    log_info "Waiting for ${resource} in ${namespace} to be ready (timeout: ${timeout})..."
    kubectl rollout status "${resource}" -n "${namespace}" --timeout="${timeout}"
    log_success "${resource} is ready."
}

wait_for_pod_ready() {
    local label="$1"
    local namespace="$2"
    local timeout="${3:-120}"
    log_info "Waiting for pod with label '${label}' in ${namespace}..."
    kubectl wait --for=condition=ready pod -l "${label}" -n "${namespace}" --timeout="${timeout}s"
    log_success "Pod with label '${label}' is ready."
}

wait_for_job() {
    local job_name="$1"
    local namespace="$2"
    local timeout="${3:-120}"
    log_info "Waiting for job '${job_name}' in ${namespace} to complete..."
    kubectl wait --for=condition=complete "job/${job_name}" -n "${namespace}" --timeout="${timeout}s"
    log_success "Job '${job_name}' completed."
}

# -- Apply manifests ----------------------------------------------------------
apply_manifests() {
    log_info "Applying Kubernetes manifests..."

    # 1. Namespace
    log_info "Creating namespace..."
    kubectl apply -f "${K8S_DIR}/namespace.yaml"
    log_success "Namespace 'orbitpay' created."

    # 2. Vault secret (must exist before StatefulSet)
    log_info "Creating Vault dev secret..."
    kubectl apply -f "${K8S_DIR}/vault/secret.yaml"
    log_success "Vault secret created."

    # 3. Vault ConfigMap, StatefulSet, Service, NetworkPolicy
    log_info "Deploying Vault..."
    kubectl apply -f "${K8S_DIR}/vault/configmap.yaml"
    kubectl apply -f "${K8S_DIR}/vault/statefulset.yaml"
    kubectl apply -f "${K8S_DIR}/vault/service.yaml"
    kubectl apply -f "${K8S_DIR}/vault/networkpolicy.yaml"

    # 4. Wait for Vault to be ready
    wait_for_pod_ready "app.kubernetes.io/name=vault" "orbitpay" 120

    # 5. Run Vault init job
    log_info "Running Vault initialization job..."
    # Delete previous job if it exists (jobs are immutable)
    kubectl delete job vault-init -n orbitpay --ignore-not-found=true
    kubectl apply -f "${K8S_DIR}/vault/init-job.yaml"
    wait_for_job "vault-init" "orbitpay" 120
    log_success "Vault initialized and configured."

    # 6. VaultGateway mock
    log_info "Deploying VaultGateway mock..."
    kubectl apply -f "${K8S_DIR}/vaultgateway-mock/secret.yaml"
    kubectl apply -f "${K8S_DIR}/vaultgateway-mock/configmap.yaml"
    kubectl apply -f "${K8S_DIR}/vaultgateway-mock/deployment.yaml"
    kubectl apply -f "${K8S_DIR}/vaultgateway-mock/service.yaml"
    kubectl apply -f "${K8S_DIR}/vaultgateway-mock/networkpolicy.yaml"
    log_success "VaultGateway mock deployed."

    # 7. Payment Gateway Connector
    log_info "Deploying Payment Gateway Connector..."
    kubectl apply -f "${K8S_DIR}/payment-gateway-connector/serviceaccount.yaml"
    kubectl apply -f "${K8S_DIR}/payment-gateway-connector/secret.yaml"
    kubectl apply -f "${K8S_DIR}/payment-gateway-connector/configmap.yaml"
    kubectl apply -f "${K8S_DIR}/payment-gateway-connector/deployment.yaml"
    kubectl apply -f "${K8S_DIR}/payment-gateway-connector/service.yaml"
    kubectl apply -f "${K8S_DIR}/payment-gateway-connector/networkpolicy.yaml"
    log_success "Payment Gateway Connector deployed."

    # 8. Monitoring stack
    log_info "Deploying monitoring stack..."

    # Prometheus
    kubectl apply -f "${K8S_DIR}/monitoring/prometheus/configmap.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/prometheus/rules.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/prometheus/deployment.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/prometheus/service.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/prometheus/networkpolicy.yaml"

    # Grafana
    kubectl apply -f "${K8S_DIR}/monitoring/grafana/secret.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/grafana/configmap-datasource.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/grafana/configmap-dashboard.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/grafana/deployment.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/grafana/service.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/grafana/networkpolicy.yaml"

    # Alertmanager
    kubectl apply -f "${K8S_DIR}/monitoring/alertmanager/configmap.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/alertmanager/deployment.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/alertmanager/service.yaml"
    kubectl apply -f "${K8S_DIR}/monitoring/alertmanager/networkpolicy.yaml"

    log_success "Monitoring stack deployed."

    # 9. Wait for all deployments
    log_info "Waiting for all deployments to be ready..."
    # Wait for each deployment — fail loudly if any deployment fails to roll out
    wait_for_rollout "deployment/vaultgateway-mock" "orbitpay" "180s"
    wait_for_rollout "deployment/payment-gateway-connector" "orbitpay" "180s"
    wait_for_rollout "deployment/prometheus" "orbitpay" "180s"
    wait_for_rollout "deployment/grafana" "orbitpay" "180s"
    wait_for_rollout "deployment/alertmanager" "orbitpay" "180s"

    log_success "All deployments are ready."
}

# -- Print access info --------------------------------------------------------
print_access_info() {
    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}  OrbitPay Infrastructure Setup Complete${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
    echo -e "${BLUE}Access services via kubectl port-forward:${NC}"
    echo ""
    echo "  Grafana (dashboards):"
    echo "    kubectl port-forward -n orbitpay svc/grafana 3000:3000"
    echo "    URL:  http://localhost:3000"
    echo "    User: admin / orbitpay-grafana-dev"
    echo ""
    echo "  Prometheus (metrics):"
    echo "    kubectl port-forward -n orbitpay svc/prometheus 9090:9090"
    echo "    URL:  http://localhost:9090"
    echo ""
    echo "  Payment Gateway Connector:"
    echo "    kubectl port-forward -n orbitpay svc/payment-gateway-connector 8000:8000"
    echo "    URL:  http://localhost:8000"
    echo ""
    echo "  Vault (secrets):"
    echo "    kubectl port-forward -n orbitpay svc/vault 8200:8200"
    echo "    URL:  http://localhost:8200"
    echo "    Token: dev-root-token-orbitpay"
    echo ""
    echo -e "${BLUE}Useful commands:${NC}"
    echo "  kubectl get pods -n orbitpay"
    echo "  kubectl logs -n orbitpay -l app.kubernetes.io/name=payment-gateway-connector"
    echo "  ./scripts/demo.sh           # Run the full demo"
    echo "  ./scripts/rotate-credentials.sh  # Rotate credentials"
    echo "  ./scripts/teardown.sh        # Destroy the cluster"
    echo ""
}

# -- Main ---------------------------------------------------------------------
main() {
    echo ""
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  OrbitPay Infrastructure Setup${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo ""

    check_prerequisites
    create_cluster
    build_and_load_images
    apply_manifests
    print_access_info
}

main "$@"
