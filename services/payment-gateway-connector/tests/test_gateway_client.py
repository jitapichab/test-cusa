"""Tests for the VaultGateway API client with circuit breaker."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry

from app.credential_manager import Credential, CredentialManager
from app.gateway_client import (
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CircuitBreaker,
    CircuitState,
    GatewayAuthError,
    GatewayClient,
    GatewayUnavailableError,
)


@pytest.fixture()
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture()
def primary_credential() -> Credential:
    return Credential(
        api_key="primary-key",
        api_secret="primary-secret",
        version=2,
        loaded_at=time.time(),
    )


@pytest.fixture()
def fallback_credential() -> Credential:
    return Credential(
        api_key="fallback-key",
        api_secret="fallback-secret",
        version=1,
        loaded_at=time.time() - 60,
    )


@pytest.fixture()
def mock_cred_manager(
    primary_credential: Credential,
    fallback_credential: Credential,
) -> MagicMock:
    manager = MagicMock(spec=CredentialManager)
    manager.get_current_credential.return_value = primary_credential
    manager.get_fallback_credential.return_value = fallback_credential
    return manager


def _make_response(
    status_code: int = 200,
    json_data: dict | None = None,
) -> httpx.Response:
    """Create a mock httpx.Response."""
    data = json_data or {
        "transaction_id": "txn-mock-001",
        "status": "approved",
    }
    response = httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("POST", "http://mock/api/v1/transactions/authorize"),
    )
    return response


class TestAuthorizePayment:
    """Tests for GatewayClient.authorize_payment."""

    @pytest.mark.asyncio
    async def test_authorize_payment_success(
        self,
        mock_cred_manager: MagicMock,
        registry: CollectorRegistry,
    ) -> None:
        """Successful authorization returns gateway response."""
        client = GatewayClient(
            credential_manager=mock_cred_manager,
            registry=registry,
            base_url="http://mock-gw:8080",
        )

        mock_response = _make_response(200)

        with patch("app.gateway_client.httpx.AsyncClient") as mock_httpx:
            mock_async_client = AsyncMock()
            mock_async_client.post.return_value = mock_response
            mock_async_client.__aenter__ = AsyncMock(
                return_value=mock_async_client
            )
            mock_async_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx.return_value = mock_async_client

            result = await client.authorize_payment(
                merchant_id="merchant-001",
                amount=50.0,
                currency="USD",
                card_token="tok_test",
                idempotency_key="idem-001",
            )

        assert result["status"] == "approved"
        assert result["transaction_id"] == "txn-mock-001"

    @pytest.mark.asyncio
    async def test_auth_failure_fallback_to_secondary_credential(
        self,
        mock_cred_manager: MagicMock,
        registry: CollectorRegistry,
    ) -> None:
        """On 401, falls back to previous credential."""
        client = GatewayClient(
            credential_manager=mock_cred_manager,
            registry=registry,
            base_url="http://mock-gw:8080",
        )

        auth_fail_response = _make_response(401, {"error": "unauthorized"})
        success_response = _make_response(
            200, {"transaction_id": "txn-fallback", "status": "approved"}
        )

        with patch("app.gateway_client.httpx.AsyncClient") as mock_httpx:
            mock_async_client = AsyncMock()
            mock_async_client.post.side_effect = [
                auth_fail_response,
                success_response,
            ]
            mock_async_client.__aenter__ = AsyncMock(
                return_value=mock_async_client
            )
            mock_async_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx.return_value = mock_async_client

            result = await client.authorize_payment(
                merchant_id="merchant-001",
                amount=50.0,
                currency="USD",
                card_token="tok_test",
                idempotency_key="idem-002",
            )

        assert result["transaction_id"] == "txn-fallback"

    @pytest.mark.asyncio
    async def test_all_credentials_rejected(
        self,
        mock_cred_manager: MagicMock,
        registry: CollectorRegistry,
    ) -> None:
        """Raises GatewayAuthError when all credentials are rejected."""
        client = GatewayClient(
            credential_manager=mock_cred_manager,
            registry=registry,
            base_url="http://mock-gw:8080",
        )

        auth_fail = _make_response(403, {"error": "forbidden"})

        with patch("app.gateway_client.httpx.AsyncClient") as mock_httpx:
            mock_async_client = AsyncMock()
            mock_async_client.post.return_value = auth_fail
            mock_async_client.__aenter__ = AsyncMock(
                return_value=mock_async_client
            )
            mock_async_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx.return_value = mock_async_client

            with pytest.raises(GatewayAuthError):
                await client.authorize_payment(
                    merchant_id="merchant-001",
                    amount=50.0,
                    currency="USD",
                    card_token="tok_test",
                    idempotency_key="idem-003",
                )

    @pytest.mark.asyncio
    async def test_timeout_handling(
        self,
        mock_cred_manager: MagicMock,
        registry: CollectorRegistry,
    ) -> None:
        """Timeout raises GatewayUnavailableError and trips circuit breaker."""
        client = GatewayClient(
            credential_manager=mock_cred_manager,
            registry=registry,
            base_url="http://mock-gw:8080",
        )

        with patch("app.gateway_client.httpx.AsyncClient") as mock_httpx:
            mock_async_client = AsyncMock()
            mock_async_client.post.side_effect = httpx.TimeoutException(
                "request timed out"
            )
            mock_async_client.__aenter__ = AsyncMock(
                return_value=mock_async_client
            )
            mock_async_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx.return_value = mock_async_client

            with pytest.raises(GatewayUnavailableError, match="timeout"):
                await client.authorize_payment(
                    merchant_id="merchant-001",
                    amount=50.0,
                    currency="USD",
                    card_token="tok_test",
                    idempotency_key="idem-004",
                )


class TestCircuitBreaker:
    """Tests for the circuit breaker pattern."""

    def test_circuit_breaker_opens_after_failures(self) -> None:
        """Circuit opens after CIRCUIT_BREAKER_FAILURE_THRESHOLD failures."""
        cb = CircuitBreaker(failure_threshold=5, timeout_seconds=30.0)
        assert cb.state == CircuitState.CLOSED

        for _ in range(5):
            cb.record_failure()

        assert cb.state == CircuitState.OPEN
        assert cb.allows_request is False

    def test_circuit_breaker_half_open_recovery(self) -> None:
        """Circuit transitions to half_open after timeout, closes on success."""
        cb = CircuitBreaker(failure_threshold=5, timeout_seconds=0.1)

        for _ in range(5):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb._last_failure_time = time.time() - 1.0
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allows_request is True

        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_circuit_breaker_half_open_failure_reopens(self) -> None:
        """Failure in half_open state reopens the circuit."""
        cb = CircuitBreaker(failure_threshold=5, timeout_seconds=0.1)

        for _ in range(5):
            cb.record_failure()

        cb._last_failure_time = time.time() - 1.0
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_open_blocks_requests(
        self,
        mock_cred_manager: MagicMock,
        registry: CollectorRegistry,
    ) -> None:
        """Requests are blocked when circuit is open."""
        client = GatewayClient(
            credential_manager=mock_cred_manager,
            registry=registry,
            base_url="http://mock-gw:8080",
        )

        for _ in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            client._circuit_breaker.record_failure()

        with pytest.raises(GatewayUnavailableError, match="Circuit breaker"):
            await client.authorize_payment(
                merchant_id="merchant-001",
                amount=50.0,
                currency="USD",
                card_token="tok_test",
                idempotency_key="idem-cb",
            )
