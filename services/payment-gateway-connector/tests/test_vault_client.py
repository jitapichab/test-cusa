"""Tests for the Vault client wrapper."""

import logging
from unittest.mock import MagicMock, patch

import hvac.exceptions
import pytest

from app.vault_client import (
    VaultAuthError,
    VaultClient,
    VaultConnectionError,
    VaultSecretNotFoundError,
    SecretData,
)


@pytest.fixture()
def mock_hvac_client() -> MagicMock:
    """Provide a mock hvac.Client with default successful responses."""
    client = MagicMock()
    client.is_authenticated.return_value = True
    client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {
            "data": {
                "api_key": "vault-api-key",
                "api_secret": "vault-api-secret",
            },
            "metadata": {
                "version": 3,
                "created_time": "2025-06-01T12:00:00Z",
            },
        }
    }
    return client


class TestReadSecret:
    """Tests for VaultClient.read_secret."""

    def test_read_secret_success(self, mock_hvac_client: MagicMock) -> None:
        """Successfully reads a secret from Vault KV v2."""
        vault = VaultClient(auth_method="token", token="test-token")
        vault._client = mock_hvac_client

        result = vault.read_secret("secret/data/orbitpay/vaultgateway")

        assert isinstance(result, SecretData)
        assert result.data["api_key"] == "vault-api-key"
        assert result.data["api_secret"] == "vault-api-secret"
        assert result.version == 3
        assert result.created_time == "2025-06-01T12:00:00Z"

    def test_vault_connection_error_retry(
        self, mock_hvac_client: MagicMock
    ) -> None:
        """Retries on transient connection errors with exponential backoff."""
        mock_hvac_client.secrets.kv.v2.read_secret_version.side_effect = [
            hvac.exceptions.VaultError("connection refused"),
            hvac.exceptions.VaultError("connection refused"),
            {
                "data": {
                    "data": {"api_key": "k", "api_secret": "s"},
                    "metadata": {"version": 1, "created_time": "2025-01-01T00:00:00Z"},
                }
            },
        ]

        vault = VaultClient(auth_method="token", token="test-token")
        vault._client = mock_hvac_client

        with patch("app.vault_client.time.sleep"):
            result = vault.read_secret("secret/data/test/path")

        assert result.data["api_key"] == "k"
        assert mock_hvac_client.secrets.kv.v2.read_secret_version.call_count == 3

    def test_vault_connection_error_exhausts_retries(
        self, mock_hvac_client: MagicMock
    ) -> None:
        """Raises VaultConnectionError after max retries."""
        mock_hvac_client.secrets.kv.v2.read_secret_version.side_effect = (
            hvac.exceptions.VaultError("connection refused")
        )

        vault = VaultClient(auth_method="token", token="test-token")
        vault._client = mock_hvac_client

        with patch("app.vault_client.time.sleep"):
            with pytest.raises(VaultConnectionError):
                vault.read_secret("secret/data/test/path")

    def test_vault_auth_failure(self, mock_hvac_client: MagicMock) -> None:
        """VaultAuthError is raised immediately, not retried."""
        mock_hvac_client.secrets.kv.v2.read_secret_version.side_effect = (
            hvac.exceptions.Forbidden("access denied")
        )

        vault = VaultClient(auth_method="token", token="test-token")
        vault._client = mock_hvac_client

        with pytest.raises(VaultAuthError):
            vault.read_secret("secret/data/test/path")

        assert (
            mock_hvac_client.secrets.kv.v2.read_secret_version.call_count == 1
        )

    def test_secret_not_found(self, mock_hvac_client: MagicMock) -> None:
        """VaultSecretNotFoundError on invalid path."""
        mock_hvac_client.secrets.kv.v2.read_secret_version.side_effect = (
            hvac.exceptions.InvalidPath("not found")
        )

        vault = VaultClient(auth_method="token", token="test-token")
        vault._client = mock_hvac_client

        with pytest.raises(VaultSecretNotFoundError):
            vault.read_secret("secret/data/nonexistent")


class TestAuditLogging:
    """Tests that audit logging works correctly and never leaks secrets."""

    def test_audit_log_on_secret_access(
        self, mock_hvac_client: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Audit log is emitted on secret access with no secret values."""
        vault = VaultClient(auth_method="token", token="test-token")
        vault._client = mock_hvac_client

        with caplog.at_level(logging.INFO, logger="audit"):
            result = vault.read_secret("secret/data/orbitpay/vaultgateway")

        assert result.data["api_key"] == "vault-api-key"

        log_text = caplog.text
        assert "vault-api-key" not in log_text
        assert "vault-api-secret" not in log_text

    def test_audit_log_on_failure(
        self, mock_hvac_client: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Audit failure log is emitted when secret read fails."""
        mock_hvac_client.secrets.kv.v2.read_secret_version.side_effect = (
            hvac.exceptions.Forbidden("denied")
        )

        vault = VaultClient(auth_method="token", token="test-token")
        vault._client = mock_hvac_client

        with caplog.at_level(logging.INFO, logger="audit"):
            with pytest.raises(VaultAuthError):
                vault.read_secret("secret/data/test/path")
