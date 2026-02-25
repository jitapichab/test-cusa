"""Tests for the FastAPI application endpoints."""

from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from app.credential_manager import CredentialManager, CredentialStatus
from app.gateway_client import GatewayAuthError, GatewayUnavailableError


VALID_PAYMENT = {
    "merchant_id": "merchant-001",
    "amount": 99.99,
    "currency": "USD",
    "card_token": "tok_test_abc",
    "idempotency_key": "idem-001",
}


class TestAuthorize:
    """Tests for POST /api/v1/authorize."""

    def test_authorize_success(self, test_app: TestClient) -> None:
        """Successful payment authorization returns 200 with approved status."""
        response = test_app.post(
            "/api/v1/authorize",
            json=VALID_PAYMENT,
            headers={"X-API-Key": "default-api-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"
        assert data["merchant_id"] == "merchant-001"
        assert data["amount"] == 99.99
        assert data["currency"] == "USD"
        assert "transaction_id" in data
        assert "timestamp" in data

    def test_authorize_invalid_api_key(self, test_app: TestClient) -> None:
        """Request with invalid API key is rejected (fail-closed)."""
        response = test_app.post(
            "/api/v1/authorize",
            json=VALID_PAYMENT,
            headers={"X-API-Key": "bad-key"},
        )
        assert response.status_code == 403
        assert "Invalid API key" in response.json()["detail"]

    def test_authorize_missing_api_key(self, test_app: TestClient) -> None:
        """Request without API key header is rejected."""
        response = test_app.post(
            "/api/v1/authorize",
            json=VALID_PAYMENT,
        )
        assert response.status_code == 401

    def test_authorize_vault_unavailable_returns_503(
        self,
        mock_vault_client: MagicMock,
        prometheus_registry: CollectorRegistry,
        test_app: TestClient,
    ) -> None:
        """When credentials are unavailable, return 503 (fail-closed)."""
        from app import main as main_module

        mock_cred_manager = MagicMock(spec=CredentialManager)
        mock_cred_manager.has_valid_credentials = False
        mock_cred_manager.credential_status = CredentialStatus.ERROR

        main_module._credential_manager = mock_cred_manager

        response = test_app.post(
            "/api/v1/authorize",
            json=VALID_PAYMENT,
            headers={"X-API-Key": "default-api-key"},
        )
        assert response.status_code == 503

    def test_authorize_expired_credentials_returns_503(
        self,
        mock_vault_client: MagicMock,
        prometheus_registry: CollectorRegistry,
        test_app: TestClient,
    ) -> None:
        """Expired credentials cause 503 (fail-closed)."""
        from app import main as main_module

        mock_cred_manager = MagicMock(spec=CredentialManager)
        mock_cred_manager.has_valid_credentials = False
        mock_cred_manager.credential_status = CredentialStatus.EXPIRED

        main_module._credential_manager = mock_cred_manager

        response = test_app.post(
            "/api/v1/authorize",
            json=VALID_PAYMENT,
            headers={"X-API-Key": "default-api-key"},
        )
        assert response.status_code == 503

    def test_authorize_gateway_unavailable_returns_503(
        self,
        test_app: TestClient,
    ) -> None:
        """Gateway unavailability returns 503."""
        from app import main as main_module

        mock_gw = AsyncMock()
        mock_gw.authorize_payment = AsyncMock(
            side_effect=GatewayUnavailableError("circuit open")
        )
        main_module._gateway_client = mock_gw

        response = test_app.post(
            "/api/v1/authorize",
            json={**VALID_PAYMENT, "idempotency_key": "idem-gw-unavail"},
            headers={"X-API-Key": "default-api-key"},
        )
        assert response.status_code == 503


class TestHealth:
    """Tests for health endpoints."""

    def test_health_live(self, test_app: TestClient) -> None:
        """Liveness always returns 200."""
        response = test_app.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_health_ready_all_ok(self, test_app: TestClient) -> None:
        """Readiness returns 200 when all components are healthy."""
        response = test_app.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["vault_connected"] is True
        assert data["credential_status"] == "valid"

    def test_health_ready_vault_down(
        self,
        mock_vault_client: MagicMock,
        test_app: TestClient,
    ) -> None:
        """Readiness returns 503 when Vault is down."""
        mock_vault_client.is_authenticated.return_value = False

        response = test_app.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["vault_connected"] is False


class TestIdempotency:
    """Tests for idempotency key deduplication."""

    def test_idempotency_key_dedup(self, test_app: TestClient) -> None:
        """Duplicate idempotency key returns cached response."""
        payload = {**VALID_PAYMENT, "idempotency_key": "idem-dedup-test"}

        response1 = test_app.post(
            "/api/v1/authorize",
            json=payload,
            headers={"X-API-Key": "default-api-key"},
        )
        assert response1.status_code == 200
        data1 = response1.json()

        response2 = test_app.post(
            "/api/v1/authorize",
            json=payload,
            headers={"X-API-Key": "default-api-key"},
        )
        assert response2.status_code == 200
        data2 = response2.json()

        assert data1["transaction_id"] == data2["transaction_id"]
        assert data1["status"] == data2["status"]


class TestDocsSecurity:
    """Tests that documentation endpoints are disabled."""

    def test_docs_endpoints_disabled(self, test_app: TestClient) -> None:
        """OpenAPI/docs/redoc endpoints return 404."""
        for path in ["/docs", "/redoc", "/openapi.json"]:
            response = test_app.get(path)
            assert response.status_code in (404, 405), (
                f"{path} should be disabled but returned {response.status_code}"
            )
