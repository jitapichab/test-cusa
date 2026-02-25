#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# OrbitPay Infrastructure Teardown Script
# Cleanly removes the OrbitPay kind cluster and optionally cleans Docker images.
# =============================================================================

# -- Colors -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# -- Globals ------------------------------------------------------------------
CLUSTER_NAME="orbitpay"
CLEAN_IMAGES=false

# -- Helpers ------------------------------------------------------------------
log_info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

cleanup() {
    local exit_code=$?
    if [[ ${exit_code} -ne 0 ]]; then
        log_error "Teardown encountered an error (exit code ${exit_code})."
    fi
    exit "${exit_code}"
}
trap cleanup ERR

usage() {
    echo "Usage: $0 [--clean]"
    echo ""
    echo "Options:"
    echo "  --clean    Also remove locally built Docker images"
    echo "  --help     Show this help message"
}

# -- Parse arguments ----------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --clean)
                CLEAN_IMAGES=true
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
    done
}

# -- Prerequisite checks -----------------------------------------------------
check_prerequisites() {
    if ! command -v kind &>/dev/null; then
        log_error "kind is not installed. Cannot proceed with teardown."
        exit 1
    fi

    if ! command -v docker &>/dev/null; then
        log_warn "docker is not installed. Cannot clean images."
    fi
}

# -- Delete cluster -----------------------------------------------------------
delete_cluster() {
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        log_info "Deleting kind cluster '${CLUSTER_NAME}'..."
        kind delete cluster --name "${CLUSTER_NAME}"
        log_success "Kind cluster '${CLUSTER_NAME}' deleted."
    else
        log_warn "Kind cluster '${CLUSTER_NAME}' does not exist. Nothing to delete."
    fi
}

# -- Clean Docker images ------------------------------------------------------
clean_images() {
    if [[ "${CLEAN_IMAGES}" != "true" ]]; then
        return 0
    fi

    if ! command -v docker &>/dev/null; then
        log_warn "docker not available. Skipping image cleanup."
        return 0
    fi

    log_info "Removing locally built Docker images..."

    local images=(
        "payment-gateway-connector:latest"
        "vaultgateway-mock:latest"
    )

    for img in "${images[@]}"; do
        if docker image inspect "${img}" &>/dev/null; then
            docker rmi "${img}" --force
            log_success "Removed image: ${img}"
        else
            log_warn "Image '${img}' not found locally. Skipping."
        fi
    done

    log_success "Docker image cleanup complete."
}

# -- Main ---------------------------------------------------------------------
main() {
    echo ""
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  OrbitPay Infrastructure Teardown${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo ""

    parse_args "$@"
    check_prerequisites
    delete_cluster
    clean_images

    echo ""
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${GREEN}  OrbitPay teardown complete.${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo ""
}

main "$@"
