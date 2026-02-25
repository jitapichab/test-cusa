"""Pydantic models for request/response payloads and health status."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class PaymentRequest(BaseModel):
    """Incoming payment authorization request."""

    merchant_id: str = Field(
        ..., min_length=1, max_length=64, description="Merchant identifier"
    )
    amount: float = Field(..., gt=0, le=1_000_000, description="Transaction amount")
    currency: str = Field(
        ..., min_length=3, max_length=3, description="ISO 4217 currency code"
    )
    card_token: str = Field(
        ..., min_length=1, max_length=128, description="Tokenized card reference"
    )
    idempotency_key: str = Field(
        ..., min_length=1, max_length=128, description="Unique key for idempotent processing"
    )


class PaymentStatus(str, Enum):
    """Possible payment authorization outcomes."""

    APPROVED = "approved"
    DECLINED = "declined"
    ERROR = "error"


class PaymentResponse(BaseModel):
    """Payment authorization response."""

    transaction_id: str
    status: PaymentStatus
    merchant_id: str
    amount: float
    currency: str
    timestamp: datetime


class HealthStatus(BaseModel):
    """Service health check response."""

    status: str
    vault_connected: bool
    credential_status: str
    gateway_reachable: bool
    uptime_seconds: float


class CredentialInfo(BaseModel):
    """Non-sensitive credential metadata."""

    version: int | None
    age_seconds: float | None
    status: str
    last_rotation: str | None
