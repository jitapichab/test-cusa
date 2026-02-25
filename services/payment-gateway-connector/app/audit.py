"""Structured audit logging for security-sensitive operations.

NEVER logs secret values -- only logs secret references (path, version).
All audit events are emitted as structured JSON.
"""

import logging
import time
from typing import Any

import structlog

AUDIT_LOGGER_NAME = "audit"

_audit_logger: structlog.stdlib.BoundLogger | None = None


def _setup_audit_logger() -> None:
    """Configure the dedicated audit logger with JSON output."""
    audit_log = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_log.setLevel(logging.INFO)

    if not audit_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit_log.addHandler(handler)
        audit_log.propagate = False


def get_audit_logger() -> structlog.stdlib.BoundLogger:
    """Return the singleton structured audit logger."""
    global _audit_logger
    if _audit_logger is None:
        _setup_audit_logger()
        _audit_logger = structlog.wrap_logger(
            logging.getLogger(AUDIT_LOGGER_NAME),
            processors=[
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
        )
    return _audit_logger


def audit_event(
    event: str,
    actor: str,
    resource: str,
    action: str,
    outcome: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a structured audit log entry.

    Args:
        event: High-level event category (e.g. credential_access, rotation).
        actor: Who/what initiated the action (service name, request ID).
        resource: The resource being acted upon (secret path, endpoint).
        action: The specific action taken (read, rotate, authorize).
        outcome: Result of the action (success, failure, denied).
        metadata: Additional context. MUST NOT contain secret values.
    """
    logger = get_audit_logger()
    safe_metadata = metadata or {}
    logger.info(
        event,
        actor=actor,
        resource=resource,
        action=action,
        outcome=outcome,
        metadata=safe_metadata,
        audit_timestamp=time.time(),
    )


def audit_credential_access(
    secret_path: str,
    version: int | None,
    outcome: str,
    actor: str = "credential_manager",
) -> None:
    """Log a credential access event. Never logs the credential value."""
    audit_event(
        event="credential_access",
        actor=actor,
        resource=secret_path,
        action="read",
        outcome=outcome,
        metadata={"secret_version": version},
    )


def audit_rotation_event(
    secret_path: str,
    old_version: int | None,
    new_version: int | None,
    outcome: str,
) -> None:
    """Log a credential rotation event."""
    audit_event(
        event="credential_rotation",
        actor="credential_manager",
        resource=secret_path,
        action="rotate",
        outcome=outcome,
        metadata={
            "old_version": old_version,
            "new_version": new_version,
        },
    )


def audit_auth_failure(
    resource: str,
    reason: str,
    actor: str = "unknown",
    request_id: str | None = None,
) -> None:
    """Log an authentication or authorization failure."""
    audit_event(
        event="auth_failure",
        actor=actor,
        resource=resource,
        action="authenticate",
        outcome="failure",
        metadata={"reason": reason, "request_id": request_id},
    )


def audit_health_check(
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Log a health check result."""
    audit_event(
        event="health_check",
        actor="system",
        resource="service",
        action="health_check",
        outcome=outcome,
        metadata=details or {},
    )
