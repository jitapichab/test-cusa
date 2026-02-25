"""Vault client wrapper for reading secrets from HashiCorp Vault KV v2.

Handles authentication, retries with exponential backoff, and audit logging.
Fails closed on any Vault unavailability.
"""

import time
from dataclasses import dataclass, field
from typing import Any

import hvac
import hvac.exceptions
import structlog

from app.audit import audit_credential_access
from app.config import settings

logger = structlog.get_logger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 0.5


@dataclass
class SecretData:
    """Holds secret data and its metadata from Vault."""

    data: dict[str, Any] = field(default_factory=dict)
    version: int | None = None
    created_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class VaultClient:
    """Wrapper around hvac for Vault KV v2 secret reads.

    Supports kubernetes auth and token auth (for local development).
    Retries with exponential backoff on transient failures.
    Audit logs every secret access attempt.
    """

    def __init__(
        self,
        addr: str | None = None,
        auth_method: str | None = None,
        role: str | None = None,
        token: str | None = None,
    ) -> None:
        self._addr = addr or settings.vault_addr
        self._auth_method = auth_method or settings.vault_auth_method
        self._role = role or settings.vault_role
        self._token = token or settings.vault_token
        self._client: hvac.Client | None = None

    def _create_client(self) -> hvac.Client:
        """Create and authenticate an hvac client."""
        client = hvac.Client(url=self._addr)

        if self._auth_method == "token" and self._token:
            client.token = self._token
        elif self._auth_method == "kubernetes":
            try:
                with open(
                    "/var/run/secrets/kubernetes.io/serviceaccount/token",
                    "r",
                ) as f:
                    jwt = f.read().strip()
                client.auth.kubernetes.login(role=self._role, jwt=jwt)
            except FileNotFoundError:
                logger.error(
                    "kubernetes_sa_token_not_found",
                    path="/var/run/secrets/kubernetes.io/serviceaccount/token",
                )
                raise VaultConnectionError(
                    "Kubernetes service account token not found"
                )
            except hvac.exceptions.VaultError as exc:
                logger.error("vault_kubernetes_auth_failed", error=str(exc))
                raise VaultAuthError(f"Kubernetes auth failed: {exc}")
        else:
            if self._token:
                client.token = self._token
            else:
                raise VaultAuthError(
                    f"Unsupported auth method '{self._auth_method}' "
                    "and no token provided"
                )

        return client

    @property
    def client(self) -> hvac.Client:
        """Lazily create and cache the authenticated client."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def is_authenticated(self) -> bool:
        """Check if the client is authenticated to Vault."""
        try:
            return bool(self.client.is_authenticated())
        except Exception:
            return False

    def read_secret(self, path: str | None = None) -> SecretData:
        """Read a secret from Vault KV v2 with retry and audit logging.

        Args:
            path: Full KV v2 path. Defaults to settings.vault_secret_path.

        Returns:
            SecretData with the secret payload and metadata.

        Raises:
            VaultConnectionError: If Vault is unreachable after retries.
            VaultAuthError: If authentication fails.
            VaultSecretNotFoundError: If the secret path does not exist.
        """
        secret_path = path or settings.vault_secret_path
        last_exception: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._read_kv2(secret_path)
                secret_data = self._parse_response(response, secret_path)

                audit_credential_access(
                    secret_path=secret_path,
                    version=secret_data.version,
                    outcome="success",
                )
                logger.info(
                    "secret_read_success",
                    path=secret_path,
                    version=secret_data.version,
                    attempt=attempt,
                )
                return secret_data

            except (VaultAuthError, VaultSecretNotFoundError):
                audit_credential_access(
                    secret_path=secret_path,
                    version=None,
                    outcome="failure",
                )
                raise

            except Exception as exc:
                last_exception = exc
                logger.warning(
                    "vault_read_retry",
                    path=secret_path,
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES:
                    backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    time.sleep(backoff)

        audit_credential_access(
            secret_path=secret_path,
            version=None,
            outcome="failure",
        )
        raise VaultConnectionError(
            f"Failed to read secret after {MAX_RETRIES} retries: "
            f"{last_exception}"
        )

    def _read_kv2(self, path: str) -> dict[str, Any]:
        """Issue the raw KV v2 read against Vault."""
        mount_point, secret_subpath = self._parse_kv2_path(path)
        try:
            response = self.client.secrets.kv.v2.read_secret_version(
                path=secret_subpath,
                mount_point=mount_point,
            )
            if response is None:
                raise VaultSecretNotFoundError(
                    f"Secret not found at path: {path}"
                )
            return dict(response)
        except hvac.exceptions.InvalidPath:
            raise VaultSecretNotFoundError(
                f"Secret not found at path: {path}"
            )
        except hvac.exceptions.Forbidden:
            raise VaultAuthError(f"Access denied for path: {path}")
        except hvac.exceptions.VaultError as exc:
            raise VaultConnectionError(f"Vault error: {exc}")

    @staticmethod
    def _parse_kv2_path(path: str) -> tuple[str, str]:
        """Parse a full KV v2 path into mount point and secret sub-path.

        Example: 'secret/data/orbitpay/vaultgateway'
            -> mount_point='secret', sub_path='orbitpay/vaultgateway'
        """
        parts = path.split("/")
        if len(parts) < 3:
            return parts[0], "/".join(parts[1:]) if len(parts) > 1 else ""

        mount_point = parts[0]
        if parts[1] == "data":
            sub_path = "/".join(parts[2:])
        else:
            sub_path = "/".join(parts[1:])
        return mount_point, sub_path

    @staticmethod
    def _parse_response(response: dict[str, Any], path: str) -> SecretData:
        """Extract data and metadata from a Vault KV v2 response."""
        try:
            data_wrapper = response.get("data", {})
            secret_payload = data_wrapper.get("data", {})
            metadata = data_wrapper.get("metadata", {})
            return SecretData(
                data=secret_payload,
                version=metadata.get("version"),
                created_time=metadata.get("created_time"),
                metadata=metadata,
            )
        except (KeyError, AttributeError, TypeError) as exc:
            raise VaultConnectionError(
                f"Unexpected Vault response format for {path}: {exc}"
            )


class VaultConnectionError(Exception):
    """Raised when Vault is unreachable or returns a transient error."""


class VaultAuthError(Exception):
    """Raised when Vault authentication fails."""


class VaultSecretNotFoundError(Exception):
    """Raised when the requested secret path does not exist."""
