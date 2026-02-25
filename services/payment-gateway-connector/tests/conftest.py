"""Shared test fixtures for payment-gateway-connector tests.

Every test gets an isolated Prometheus CollectorRegistry to prevent
metric registration conflicts across tests.
"""

from collections import OrderedDict
from typing import Any, AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from app.config import settings
from app.credential_manager import Credential, CredentialManager, CredentialStatus
from app.gateway_client import GatewayClient
from app.models import PaymentStatus
from app.vault_client import SecretData, VaultClient


@pytest.fixture()
def prometheus_registry() -> CollectorRegistry:
    """Provide an isolated Prometheus registry for each test."""
    return CollectorRegistry()


@pytest.fixture()
def mock_vault_client() -> MagicMock:
    """Provide a mock VaultClient that returns valid test credentials."""
    client = MagicMock(spec=VaultClient)
    client.is_authenticated.return_value = True
    client.read_secret.return_value = SecretData(
        data={"api_key": "test-api-key", "api_secret": "test-api-secret"},
        version=1,
        created_time="2025-01-01T00:00:00Z",
        metadata={"version": 1, "created_time": "2025-01-01T00:00:00Z"},
    )
    return client


@pytest.fixture()
def mock_credential_manager(
    mock_vault_client: MagicMock,
    prometheus_registry: CollectorRegistry,
) -> CredentialManager:
    """Provide a CredentialManager with a mocked VaultClient."""
    manager = CredentialManager(
        vault_client=mock_vault_client,
        registry=prometheus_registry,
        check_interval=300,
    )
    manager.load_credentials()
    return manager


@pytest.fixture()
def mock_gateway_client(
    mock_credential_manager: CredentialManager,
    prometheus_registry: CollectorRegistry,
) -> GatewayClient:
    """Provide a GatewayClient with mocked dependencies."""
    return GatewayClient(
        credential_manager=mock_credential_manager,
        registry=prometheus_registry,
        base_url="http://mock-gateway:8080",
    )


@pytest.fixture()
def test_app(
    mock_vault_client: MagicMock,
    mock_credential_manager: CredentialManager,
    prometheus_registry: CollectorRegistry,
) -> Generator[TestClient, None, None]:
    """Provide a TestClient with all components mocked.

    Injects mocks, patches the gateway client to return mock responses,
    and resets global state after each test.
    """
    from app import main as main_module

    mock_gw = AsyncMock(spec=GatewayClient)
    mock_gw.authorize_payment = AsyncMock(
        return_value={
            "transaction_id": "txn-test-001",
            "status": "approved",
        }
    )
    mock_gw.check_reachability = AsyncMock(return_value=True)
    mock_gw.gateway_requests_total = MagicMock()
    mock_gw.gateway_request_duration_seconds = MagicMock()
    mock_gw.gateway_auth_failures_total = MagicMock()
    mock_gw.circuit_breaker_state = MagicMock()

    original_vault = main_module._vault_client
    original_cred = main_module._credential_manager
    original_gw = main_module._gateway_client
    original_registry = main_module._metrics_registry
    original_cache = main_module._idempotency_cache
    original_start = main_module._start_time

    main_module.set_components(
        vault_client=mock_vault_client,
        credential_manager=mock_credential_manager,
        gateway_client=mock_gw,
        registry=prometheus_registry,
    )
    main_module._start_time = 1000000.0
    main_module._idempotency_cache = OrderedDict()

    # Set test API keys (config defaults to [] for fail-closed security)
    original_api_keys = settings.api_keys
    settings.api_keys = ["default-api-key"]

    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        yield client

    main_module._vault_client = original_vault
    main_module._credential_manager = original_cred
    main_module._gateway_client = original_gw
    main_module._metrics_registry = original_registry
    main_module._idempotency_cache = original_cache
    main_module._start_time = original_start
    settings.api_keys = original_api_keys
