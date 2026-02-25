"""VaultGateway API client with circuit breaker and dual-credential fallback.

Makes HTTP calls to the VaultGateway mock for payment authorization.
Implements circuit breaker pattern and retries with fallback credentials.
"""

import time
from enum import Enum
from typing import Any

import httpx
import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.audit import audit_auth_failure
from app.config import settings
from app.credential_manager import Credential, CredentialManager

logger = structlog.get_logger(__name__)

CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_TIMEOUT_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 10.0


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple circuit breaker implementation.

    closed -> open after CIRCUIT_BREAKER_FAILURE_THRESHOLD consecutive failures.
    open -> half_open after CIRCUIT_BREAKER_TIMEOUT_SECONDS.
    half_open -> closed on success, open on failure.
    """

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        timeout_seconds: float = CIRCUIT_BREAKER_TIMEOUT_SECONDS,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._timeout_seconds = timeout_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if (
                self._last_failure_time is not None
                and time.time() - self._last_failure_time
                >= self._timeout_seconds
            ):
                self._state = CircuitState.HALF_OPEN
        return self._state

    @property
    def allows_request(self) -> bool:
        current = self.state
        return current in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
        elif self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(
                "circuit_breaker_opened",
                failure_count=self._failure_count,
            )


class GatewayClient:
    """Async client for VaultGateway payment authorization API.

    Uses current credentials from CredentialManager with fallback to
    previous credentials during rotation windows.
    """

    def __init__(
        self,
        credential_manager: CredentialManager,
        registry: CollectorRegistry | None = None,
        base_url: str | None = None,
    ) -> None:
        self._credential_manager = credential_manager
        self._base_url = base_url or settings.vaultgateway_url
        self._circuit_breaker = CircuitBreaker()

        reg = registry or CollectorRegistry()
        self._registry = reg

        self.gateway_requests_total = Counter(
            "gateway_requests_total",
            "Total gateway requests by status",
            ["status"],
            registry=reg,
        )
        self.gateway_request_duration_seconds = Histogram(
            "gateway_request_duration_seconds",
            "Gateway request duration in seconds",
            registry=reg,
        )
        self.gateway_auth_failures_total = Counter(
            "gateway_auth_failures_total",
            "Total gateway authentication failures",
            registry=reg,
        )
        self.circuit_breaker_state = Gauge(
            "circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=open, 2=half_open)",
            registry=reg,
        )

    def _update_circuit_breaker_metric(self) -> None:
        state_map = {
            CircuitState.CLOSED: 0,
            CircuitState.OPEN: 1,
            CircuitState.HALF_OPEN: 2,
        }
        self.circuit_breaker_state.set(
            state_map.get(self._circuit_breaker.state, 0)
        )

    async def authorize_payment(
        self,
        merchant_id: str,
        amount: float,
        currency: str,
        card_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Send a payment authorization request to VaultGateway.

        Tries primary credential first. On 401/403, falls back to the
        previous credential (dual-credential window). Respects circuit
        breaker state.

        Returns:
            Gateway response dict with transaction_id, status, etc.

        Raises:
            GatewayUnavailableError: If the circuit is open or gateway unreachable.
            GatewayAuthError: If all credentials fail authentication.
        """
        self._update_circuit_breaker_metric()

        if not self._circuit_breaker.allows_request:
            self._update_circuit_breaker_metric()
            raise GatewayUnavailableError(
                "Circuit breaker is open; gateway requests are blocked"
            )

        primary = self._credential_manager.get_current_credential()
        fallback = self._credential_manager.get_fallback_credential()

        if primary is None:
            raise GatewayAuthError("No credentials available")

        payload = {
            "merchant_id": merchant_id,
            "amount": amount,
            "currency": currency,
            "card_token": card_token,
            "idempotency_key": idempotency_key,
        }

        result = await self._try_authorize(primary, payload)
        if result is not None:
            return result

        if fallback is not None and fallback is not primary:
            logger.info("gateway_fallback_credential_attempt")
            result = await self._try_authorize(fallback, payload)
            if result is not None:
                return result

        self.gateway_auth_failures_total.inc()
        audit_auth_failure(
            resource=f"{self._base_url}/api/v1/transactions/authorize",
            reason="all_credentials_rejected",
        )
        raise GatewayAuthError(
            "All credentials were rejected by the gateway"
        )

    async def _try_authorize(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Attempt authorization with a single credential.

        Returns the response dict on success, None on auth failure.
        Raises GatewayUnavailableError on network/timeout errors.
        """
        start_time = time.time()
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    f"{self._base_url}/api/v1/transactions/authorize",
                    json=payload,
                    headers={
                        "X-Gateway-API-Key": credential.api_key,
                        "X-Gateway-API-Secret": credential.api_secret,
                        "Content-Type": "application/json",
                    },
                )

            duration = time.time() - start_time
            self.gateway_request_duration_seconds.observe(duration)

            if response.status_code in (401, 403):
                logger.warning(
                    "gateway_auth_rejected",
                    status_code=response.status_code,
                    credential_version=credential.version,
                )
                self.gateway_requests_total.labels(
                    status=str(response.status_code)
                ).inc()
                return None

            if response.status_code >= 500:
                self._circuit_breaker.record_failure()
                self._update_circuit_breaker_metric()
                self.gateway_requests_total.labels(
                    status=str(response.status_code)
                ).inc()
                raise GatewayUnavailableError(
                    f"Gateway returned {response.status_code}"
                )

            self._circuit_breaker.record_success()
            self._update_circuit_breaker_metric()

            status_label = str(response.status_code)
            self.gateway_requests_total.labels(status=status_label).inc()

            return dict(response.json())

        except httpx.TimeoutException as exc:
            duration = time.time() - start_time
            self.gateway_request_duration_seconds.observe(duration)
            self._circuit_breaker.record_failure()
            self._update_circuit_breaker_metric()
            self.gateway_requests_total.labels(status="timeout").inc()
            logger.error("gateway_timeout", error=str(exc))
            raise GatewayUnavailableError(f"Gateway timeout: {exc}")

        except httpx.HTTPError as exc:
            duration = time.time() - start_time
            self.gateway_request_duration_seconds.observe(duration)
            self._circuit_breaker.record_failure()
            self._update_circuit_breaker_metric()
            self.gateway_requests_total.labels(status="error").inc()
            logger.error("gateway_http_error", error=str(exc))
            raise GatewayUnavailableError(f"Gateway error: {exc}")

    async def check_reachability(self) -> bool:
        """Check if the gateway is reachable via its health endpoint."""
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS
            ) as client:
                response = await client.get(f"{self._base_url}/health")
                return response.status_code == 200
        except Exception:
            return False


class GatewayUnavailableError(Exception):
    """Raised when the gateway is unreachable or circuit is open."""


class GatewayAuthError(Exception):
    """Raised when all credentials are rejected by the gateway."""
