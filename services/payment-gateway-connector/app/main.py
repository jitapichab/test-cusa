"""FastAPI application for the payment-gateway-connector service.

Exposes payment authorization, health, and metrics endpoints.
All credential access goes through Vault; fail-closed on errors.
Docs endpoints are disabled for security.
"""

import asyncio
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    CollectorRegistry,
    generate_latest,
)

from app.audit import audit_auth_failure, audit_health_check
from app.config import settings
from app.credential_manager import CredentialManager, CredentialStatus
from app.gateway_client import (
    GatewayAuthError,
    GatewayClient,
    GatewayUnavailableError,
)
from app.middleware import (
    AuditLoggingMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from app.models import HealthStatus, PaymentRequest, PaymentResponse, PaymentStatus
from app.vault_client import VaultClient

logger = structlog.get_logger(__name__)

IDEMPOTENCY_CACHE_MAX_SIZE = 10_000

_start_time: float = 0.0
_vault_client: VaultClient | None = None
_credential_manager: CredentialManager | None = None
_gateway_client: GatewayClient | None = None
_metrics_registry: CollectorRegistry | None = None
_idempotency_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_shutdown_event: asyncio.Event | None = None
_in_flight: int = 0


def get_registry() -> CollectorRegistry:
    global _metrics_registry
    if _metrics_registry is None:
        _metrics_registry = CollectorRegistry()
    return _metrics_registry


def set_components(
    vault_client: VaultClient,
    credential_manager: CredentialManager,
    gateway_client: GatewayClient,
    registry: CollectorRegistry | None = None,
) -> None:
    """Inject dependencies (used by tests to provide mocks)."""
    global _vault_client, _credential_manager, _gateway_client, _metrics_registry
    _vault_client = vault_client
    _credential_manager = credential_manager
    _gateway_client = gateway_client
    if registry is not None:
        _metrics_registry = registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown logic."""
    global _start_time, _vault_client, _credential_manager
    global _gateway_client, _shutdown_event

    _start_time = time.time()
    _shutdown_event = asyncio.Event()

    if _vault_client is None:
        _vault_client = VaultClient()

    registry = get_registry()

    if _credential_manager is None:
        _credential_manager = CredentialManager(
            vault_client=_vault_client,
            registry=registry,
        )

    _credential_manager.load_credentials()
    await _credential_manager.start_refresh_loop()

    if _gateway_client is None:
        _gateway_client = GatewayClient(
            credential_manager=_credential_manager,
            registry=registry,
        )

    logger.info("service_started", port=settings.service_port)

    yield

    logger.info("service_shutting_down")
    _shutdown_event.set()
    await _credential_manager.stop_refresh_loop()

    deadline = time.time() + settings.graceful_shutdown_timeout
    while _in_flight > 0 and time.time() < deadline:
        await asyncio.sleep(0.1)

    if _in_flight > 0:
        logger.warning(
            "graceful_shutdown_timeout_exceeded",
            in_flight=_in_flight,
        )

    logger.info("service_stopped")


app = FastAPI(
    title="payment-gateway-connector",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuditLoggingMiddleware)
app.add_middleware(RequestIDMiddleware)


def _validate_api_key(api_key: str | None, request_id: str) -> None:
    """Validate the request API key. Fail-closed on any issue."""
    if not api_key:
        audit_auth_failure(
            resource="/api/v1/authorize",
            reason="missing_api_key",
            request_id=request_id,
        )
        raise HTTPException(status_code=401, detail="Missing API key")

    if api_key not in settings.api_keys:
        audit_auth_failure(
            resource="/api/v1/authorize",
            reason="invalid_api_key",
            request_id=request_id,
        )
        raise HTTPException(status_code=403, detail="Invalid API key")


def _check_idempotency(key: str) -> dict[str, Any] | None:
    """Check idempotency cache. Returns cached response or None."""
    if key in _idempotency_cache:
        _idempotency_cache.move_to_end(key)
        return _idempotency_cache[key]
    return None


def _store_idempotency(key: str, response_data: dict[str, Any]) -> None:
    """Store a response in the bounded idempotency cache."""
    _idempotency_cache[key] = response_data
    while len(_idempotency_cache) > IDEMPOTENCY_CACHE_MAX_SIZE:
        _idempotency_cache.popitem(last=False)


@app.post("/api/v1/authorize", response_model=PaymentResponse)
async def authorize_payment(
    payment: PaymentRequest,
    request: Request,
    x_api_key: str | None = Header(None),
) -> PaymentResponse | JSONResponse:
    """Authorize a payment through VaultGateway.

    Requires a valid X-API-Key header. Fail-closed: returns 503 on any
    credential or gateway error.
    """
    global _in_flight
    _in_flight += 1

    try:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        _validate_api_key(x_api_key, request_id)

        if _credential_manager is None or not _credential_manager.has_valid_credentials:
            raise HTTPException(
                status_code=503,
                detail="Service unavailable: credential error",
            )

        cached = _check_idempotency(payment.idempotency_key)
        if cached is not None:
            logger.info(
                "idempotency_cache_hit",
                idempotency_key=payment.idempotency_key,
            )
            return JSONResponse(content=cached)

        if _gateway_client is None:
            raise HTTPException(
                status_code=503,
                detail="Service unavailable: gateway not initialized",
            )

        try:
            gateway_response = await _gateway_client.authorize_payment(
                merchant_id=payment.merchant_id,
                amount=payment.amount,
                currency=payment.currency,
                card_token=payment.card_token,
                idempotency_key=payment.idempotency_key,
            )
        except GatewayUnavailableError as exc:
            logger.error("gateway_unavailable", error=str(exc))
            raise HTTPException(
                status_code=503,
                detail="Service unavailable: gateway unreachable",
            )
        except GatewayAuthError as exc:
            logger.error("gateway_auth_error", error=str(exc))
            raise HTTPException(
                status_code=503,
                detail="Service unavailable: credential error",
            )

        status_str = gateway_response.get("status", "error")
        try:
            payment_status = PaymentStatus(status_str)
        except ValueError:
            payment_status = PaymentStatus.ERROR

        response = PaymentResponse(
            transaction_id=gateway_response.get(
                "transaction_id", str(uuid.uuid4())
            ),
            status=payment_status,
            merchant_id=payment.merchant_id,
            amount=payment.amount,
            currency=payment.currency,
            timestamp=datetime.now(timezone.utc),
        )

        response_data = response.model_dump(mode="json")
        _store_idempotency(payment.idempotency_key, response_data)

        return response

    finally:
        _in_flight -= 1


@app.get("/health/live")
async def liveness() -> dict[str, str]:
    """Liveness probe: always 200 if the process is running."""
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness() -> JSONResponse:
    """Readiness probe: checks vault, credentials, and gateway."""
    vault_connected = False
    if _vault_client is not None:
        try:
            vault_connected = _vault_client.is_authenticated()
        except Exception:
            vault_connected = False

    cred_status = CredentialStatus.ERROR
    if _credential_manager is not None:
        cred_status = _credential_manager.credential_status

    gateway_reachable = False
    if _gateway_client is not None:
        try:
            gateway_reachable = await _gateway_client.check_reachability()
        except Exception:
            gateway_reachable = False

    uptime = time.time() - _start_time if _start_time > 0 else 0.0

    is_ready = vault_connected and cred_status in (
        CredentialStatus.VALID,
        CredentialStatus.EXPIRING_SOON,
    )

    health = HealthStatus(
        status="ready" if is_ready else "not_ready",
        vault_connected=vault_connected,
        credential_status=cred_status.value,
        gateway_reachable=gateway_reachable,
        uptime_seconds=round(uptime, 2),
    )

    audit_health_check(
        outcome="ready" if is_ready else "not_ready",
        details=health.model_dump(),
    )

    status_code = 200 if is_ready else 503
    return JSONResponse(content=health.model_dump(), status_code=status_code)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus metrics endpoint."""
    registry = get_registry()
    data = generate_latest(registry)
    return PlainTextResponse(
        content=data.decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
