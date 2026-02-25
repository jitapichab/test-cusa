"""Credential lifecycle manager for VaultGateway API credentials.

Loads credentials from Vault, periodically checks for updates, and supports
dual-credential windows during rotation. Uses bounded data structures and
exposes Prometheus metrics for credential health.
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge

from app.audit import audit_credential_access, audit_rotation_event
from app.config import settings
from app.vault_client import (
    SecretData,
    VaultClient,
    VaultConnectionError,
)

logger = structlog.get_logger(__name__)

MAX_CACHE_SIZE = 5
EXPIRY_SOON_THRESHOLD_SECONDS = 3600


class CredentialStatus(str, Enum):
    """Health status of a credential."""

    VALID = "valid"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass
class Credential:
    """A single cached credential with metadata."""

    api_key: str
    api_secret: str
    version: int | None
    loaded_at: float
    created_time: str | None = None
    ttl: int | None = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.loaded_at

    @property
    def status(self) -> CredentialStatus:
        if self.ttl is not None:
            remaining = self.ttl - self.age_seconds
            if remaining <= 0:
                return CredentialStatus.EXPIRED
            if remaining <= EXPIRY_SOON_THRESHOLD_SECONDS:
                return CredentialStatus.EXPIRING_SOON
        return CredentialStatus.VALID


class CredentialManager:
    """Manages the lifecycle of VaultGateway credentials.

    - Loads credentials from Vault on startup.
    - Periodically polls for updates.
    - Keeps a bounded cache supporting dual-credential windows.
    - Exposes Prometheus metrics.
    """

    def __init__(
        self,
        vault_client: VaultClient,
        registry: CollectorRegistry | None = None,
        check_interval: int | None = None,
    ) -> None:
        self._vault_client = vault_client
        self._check_interval = (
            check_interval
            if check_interval is not None
            else settings.credential_check_interval
        )
        self._cache: OrderedDict[int, Credential] = OrderedDict()
        self._current_version: int | None = None
        self._previous_version: int | None = None
        self._last_error: str | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._running = False

        reg = registry or CollectorRegistry()
        self._registry = reg

        self.credential_version = Gauge(
            "credential_version",
            "Current credential version from Vault",
            registry=reg,
        )
        self.credential_age_seconds = Gauge(
            "credential_age_seconds",
            "Age of the current credential in seconds",
            registry=reg,
        )
        self.credential_rotation_total = Counter(
            "credential_rotation_total",
            "Total number of credential rotations detected",
            registry=reg,
        )
        self.credential_fetch_errors_total = Counter(
            "credential_fetch_errors_total",
            "Total number of credential fetch errors",
            registry=reg,
        )

    def load_credentials(self) -> bool:
        """Synchronously load credentials from Vault.

        Returns True on success, False on failure. On failure, existing
        valid credentials are retained (fail-closed: the service will
        refuse requests if no credentials are available at all).
        """
        try:
            secret: SecretData = self._vault_client.read_secret()
            return self._process_secret(secret)
        except Exception as exc:
            self._last_error = str(exc)
            self.credential_fetch_errors_total.inc()
            logger.error("credential_load_failed", error=str(exc))
            audit_credential_access(
                secret_path=settings.vault_secret_path,
                version=None,
                outcome="failure",
            )
            return False

    def _process_secret(self, secret: SecretData) -> bool:
        """Process a secret response and update the cache."""
        api_key = secret.data.get("api_key", "")
        api_secret = secret.data.get("api_secret", "")
        version = secret.version
        ttl = secret.data.get("ttl")

        if not api_key or not api_secret:
            self._last_error = "Missing api_key or api_secret in Vault data"
            self.credential_fetch_errors_total.inc()
            logger.error("credential_missing_fields", version=version)
            return False

        if version == self._current_version:
            logger.debug("credential_unchanged", version=version)
            self._update_age_metric()
            return True

        credential = Credential(
            api_key=api_key,
            api_secret=api_secret,
            version=version,
            loaded_at=time.time(),
            created_time=secret.created_time,
            ttl=int(ttl) if ttl is not None else None,
        )

        old_version = self._current_version
        self._previous_version = old_version
        self._current_version = version

        cache_key = version if version is not None else int(time.time())
        self._cache[cache_key] = credential

        while len(self._cache) > MAX_CACHE_SIZE:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.info("credential_cache_evicted", evicted_version=evicted_key)

        if version is not None:
            self.credential_version.set(version)
        self._update_age_metric()
        self._last_error = None

        if old_version is not None and old_version != version:
            self.credential_rotation_total.inc()
            audit_rotation_event(
                secret_path=settings.vault_secret_path,
                old_version=old_version,
                new_version=version,
                outcome="success",
            )
            logger.info(
                "credential_rotated",
                old_version=old_version,
                new_version=version,
            )
        else:
            logger.info("credential_loaded", version=version)

        return True

    def _update_age_metric(self) -> None:
        """Update the credential age metric from the current credential."""
        current = self.get_current_credential()
        if current is not None:
            self.credential_age_seconds.set(current.age_seconds)

    def get_current_credential(self) -> Credential | None:
        """Return the most recently loaded credential."""
        if self._current_version is not None:
            return self._cache.get(self._current_version)
        if self._cache:
            return next(reversed(self._cache.values()))
        return None

    def get_fallback_credential(self) -> Credential | None:
        """Return the previous credential for dual-credential validation.

        During rotation, both old and new credentials should be accepted.
        """
        if self._previous_version is not None:
            return self._cache.get(self._previous_version)
        items = list(self._cache.values())
        if len(items) >= 2:
            return items[-2]
        return None

    @property
    def credential_status(self) -> CredentialStatus:
        """Overall health status of credentials."""
        current = self.get_current_credential()
        if current is None:
            return CredentialStatus.ERROR
        return current.status

    @property
    def has_valid_credentials(self) -> bool:
        """Whether the service has at least one valid credential."""
        status = self.credential_status
        return status in (CredentialStatus.VALID, CredentialStatus.EXPIRING_SOON)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def get_credential_info(self) -> dict[str, Any]:
        """Return serializable credential metadata (no secret values)."""
        current = self.get_current_credential()
        if current is None:
            return {
                "version": None,
                "age_seconds": None,
                "status": CredentialStatus.ERROR.value,
                "last_rotation": None,
            }
        return {
            "version": current.version,
            "age_seconds": current.age_seconds,
            "status": current.status.value,
            "last_rotation": current.created_time,
        }

    async def start_refresh_loop(self) -> None:
        """Start the background credential refresh loop."""
        self._running = True
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop_refresh_loop(self) -> None:
        """Stop the background credential refresh loop."""
        self._running = False
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None

    async def _refresh_loop(self) -> None:
        """Periodically refresh credentials from Vault."""
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                if not self._running:
                    break
                logger.debug("credential_refresh_tick")
                self.load_credentials()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("credential_refresh_error", error=str(exc))
                self.credential_fetch_errors_total.inc()
