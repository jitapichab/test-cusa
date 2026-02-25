"""Tests for the credential lifecycle manager."""

import time
from unittest.mock import MagicMock

import pytest
from prometheus_client import CollectorRegistry

from app.credential_manager import (
    MAX_CACHE_SIZE,
    Credential,
    CredentialManager,
    CredentialStatus,
)
from app.vault_client import SecretData, VaultClient, VaultConnectionError


@pytest.fixture()
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture()
def vault_client() -> MagicMock:
    client = MagicMock(spec=VaultClient)
    client.read_secret.return_value = SecretData(
        data={"api_key": "key-v1", "api_secret": "secret-v1"},
        version=1,
        created_time="2025-01-01T00:00:00Z",
        metadata={"version": 1},
    )
    return client


class TestLoadCredentials:
    """Tests for initial credential loading."""

    def test_load_credentials_success(
        self, vault_client: MagicMock, registry: CollectorRegistry
    ) -> None:
        """Successfully loads credentials from Vault."""
        manager = CredentialManager(
            vault_client=vault_client, registry=registry
        )
        result = manager.load_credentials()

        assert result is True
        current = manager.get_current_credential()
        assert current is not None
        assert current.api_key == "key-v1"
        assert current.api_secret == "secret-v1"
        assert current.version == 1
        assert manager.credential_status == CredentialStatus.VALID

    def test_load_credentials_missing_fields(
        self, registry: CollectorRegistry
    ) -> None:
        """Fails when Vault data is missing required fields."""
        client = MagicMock(spec=VaultClient)
        client.read_secret.return_value = SecretData(
            data={"api_key": "key-only"},
            version=1,
            created_time="2025-01-01T00:00:00Z",
            metadata={"version": 1},
        )
        manager = CredentialManager(vault_client=client, registry=registry)
        result = manager.load_credentials()

        assert result is False
        assert manager.get_current_credential() is None


class TestCredentialRotation:
    """Tests for credential rotation detection."""

    def test_credential_rotation_detected(
        self, vault_client: MagicMock, registry: CollectorRegistry
    ) -> None:
        """Detects when Vault returns a new credential version."""
        manager = CredentialManager(
            vault_client=vault_client, registry=registry
        )
        manager.load_credentials()

        vault_client.read_secret.return_value = SecretData(
            data={"api_key": "key-v2", "api_secret": "secret-v2"},
            version=2,
            created_time="2025-01-02T00:00:00Z",
            metadata={"version": 2},
        )
        manager.load_credentials()

        current = manager.get_current_credential()
        assert current is not None
        assert current.version == 2
        assert current.api_key == "key-v2"

    def test_dual_credential_window(
        self, vault_client: MagicMock, registry: CollectorRegistry
    ) -> None:
        """After rotation, both old and new credentials are available."""
        manager = CredentialManager(
            vault_client=vault_client, registry=registry
        )
        manager.load_credentials()

        vault_client.read_secret.return_value = SecretData(
            data={"api_key": "key-v2", "api_secret": "secret-v2"},
            version=2,
            created_time="2025-01-02T00:00:00Z",
            metadata={"version": 2},
        )
        manager.load_credentials()

        current = manager.get_current_credential()
        fallback = manager.get_fallback_credential()

        assert current is not None
        assert current.api_key == "key-v2"
        assert fallback is not None
        assert fallback.api_key == "key-v1"

    def test_bounded_cache_eviction(
        self, registry: CollectorRegistry
    ) -> None:
        """Cache evicts oldest entries when exceeding MAX_CACHE_SIZE."""
        client = MagicMock(spec=VaultClient)
        manager = CredentialManager(vault_client=client, registry=registry)

        for version in range(1, MAX_CACHE_SIZE + 3):
            client.read_secret.return_value = SecretData(
                data={
                    "api_key": f"key-v{version}",
                    "api_secret": f"secret-v{version}",
                },
                version=version,
                created_time=f"2025-01-{version:02d}T00:00:00Z",
                metadata={"version": version},
            )
            manager.load_credentials()

        assert len(manager._cache) == MAX_CACHE_SIZE

        assert 1 not in manager._cache
        assert 2 not in manager._cache

        latest_version = MAX_CACHE_SIZE + 2
        current = manager.get_current_credential()
        assert current is not None
        assert current.version == latest_version


class TestVaultUnavailability:
    """Tests for behavior when Vault becomes unavailable."""

    def test_vault_unavailable_keeps_last_valid(
        self, vault_client: MagicMock, registry: CollectorRegistry
    ) -> None:
        """When Vault fails, the last valid credential is retained."""
        manager = CredentialManager(
            vault_client=vault_client, registry=registry
        )
        manager.load_credentials()

        vault_client.read_secret.side_effect = VaultConnectionError(
            "Vault unreachable"
        )
        result = manager.load_credentials()

        assert result is False

        current = manager.get_current_credential()
        assert current is not None
        assert current.api_key == "key-v1"
        assert manager.has_valid_credentials is True


class TestCredentialExpiry:
    """Tests for credential expiration detection."""

    def test_credential_expiry_detection(
        self, registry: CollectorRegistry
    ) -> None:
        """Credentials with expired TTL are marked as expired."""
        client = MagicMock(spec=VaultClient)
        client.read_secret.return_value = SecretData(
            data={
                "api_key": "key-expiring",
                "api_secret": "secret-expiring",
                "ttl": "1",
            },
            version=1,
            created_time="2025-01-01T00:00:00Z",
            metadata={"version": 1},
        )
        manager = CredentialManager(vault_client=client, registry=registry)
        manager.load_credentials()

        cred = manager.get_current_credential()
        assert cred is not None
        cred.loaded_at = time.time() - 10

        assert cred.status == CredentialStatus.EXPIRED
        assert manager.credential_status == CredentialStatus.EXPIRED
