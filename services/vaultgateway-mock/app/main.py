"""VaultGateway mock service.

Simulates a payment gateway API for testing credential rotation,
dual-credential windows, and payment authorization flows.
Supports multiple valid API keys simultaneously.
"""

import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

_registry = CollectorRegistry()

requests_total = Counter(
    "requests_total",
    "Total number of requests by endpoint and status",
    ["endpoint", "status"],
    registry=_registry,
)

request_duration_seconds = Histogram(
    "request_duration_seconds",
    "Request duration in seconds",
    ["endpoint"],
    registry=_registry,
)


def _get_valid_keys() -> list[str]:
    """Load valid API keys from environment.

    Supports multiple keys separated by commas for rotation testing.
    """
    keys_raw = os.environ.get(
        "VALID_API_KEYS",
        "test-gateway-key-1,test-gateway-key-2",
    )
    return [k.strip() for k in keys_raw.split(",") if k.strip()]


class AuthorizeRequest(BaseModel):
    """Payment authorization request from the connector service."""

    merchant_id: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    currency: str = Field(..., min_length=3, max_length=3)
    card_token: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)


class AuthorizeResponse(BaseModel):
    """Payment authorization response."""

    transaction_id: str
    status: str
    merchant_id: str
    amount: float
    currency: str
    timestamp: str
    message: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    yield


app = FastAPI(
    title="vaultgateway-mock",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.post("/api/v1/transactions/authorize", response_model=AuthorizeResponse)
async def authorize_transaction(
    payload: AuthorizeRequest,
    request: Request,
    x_gateway_api_key: str | None = Header(None, alias="X-Gateway-API-Key"),
) -> AuthorizeResponse:
    """Authorize a payment transaction.

    Validates the API key against the configured list of valid keys.
    Returns approve (90%) or decline (10%) to simulate a real gateway.
    """
    start_time = time.time()
    endpoint = "/api/v1/transactions/authorize"

    if not x_gateway_api_key:
        requests_total.labels(endpoint=endpoint, status="401").inc()
        request_duration_seconds.labels(endpoint=endpoint).observe(
            time.time() - start_time
        )
        raise HTTPException(status_code=401, detail="Missing API key")

    valid_keys = _get_valid_keys()
    if x_gateway_api_key not in valid_keys:
        requests_total.labels(endpoint=endpoint, status="403").inc()
        request_duration_seconds.labels(endpoint=endpoint).observe(
            time.time() - start_time
        )
        raise HTTPException(status_code=403, detail="Invalid API key")

    is_approved = random.random() < 0.9
    status = "approved" if is_approved else "declined"
    message = (
        "Transaction approved" if is_approved else "Transaction declined"
    )

    response = AuthorizeResponse(
        transaction_id=f"txn-{uuid.uuid4().hex[:12]}",
        status=status,
        merchant_id=payload.merchant_id,
        amount=payload.amount,
        currency=payload.currency,
        timestamp=datetime.now(timezone.utc).isoformat(),
        message=message,
    )

    requests_total.labels(endpoint=endpoint, status="200").inc()
    request_duration_seconds.labels(endpoint=endpoint).observe(
        time.time() - start_time
    )

    return response


@app.get("/health")
async def health() -> dict[str, str]:
    """Simple health check endpoint."""
    return {"status": "healthy"}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus metrics endpoint."""
    data = generate_latest(_registry)
    return PlainTextResponse(
        content=data.decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
