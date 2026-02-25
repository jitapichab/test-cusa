"""Custom middleware for request tracking, audit logging, and rate limiting."""

import time
import uuid
from collections import OrderedDict
from typing import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.audit import audit_event

logger = structlog.get_logger(__name__)

MAX_RATE_LIMIT_ENTRIES = 10_000
RATE_LIMIT_REQUESTS_PER_SECOND = 100


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assigns a unique X-Request-ID to every request."""

    async def dispatch(
        self, request: Request, call_next: Callable[..., Response]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Audit logs payment endpoint access with timing."""

    async def dispatch(
        self, request: Request, call_next: Callable[..., Response]
    ) -> Response:
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time

        if request.url.path.startswith("/api/"):
            request_id = getattr(request.state, "request_id", "unknown")
            audit_event(
                event="api_access",
                actor=request_id,
                resource=request.url.path,
                action=request.method,
                outcome="success" if response.status_code < 400 else "failure",
                metadata={
                    "status_code": response.status_code,
                    "duration_seconds": round(duration, 4),
                    "method": request.method,
                    "path": request.url.path,
                },
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple token bucket rate limiter using a bounded dict.

    Tracks request counts per second per client IP. Limits to
    RATE_LIMIT_REQUESTS_PER_SECOND requests per second.
    """

    def __init__(self, app: Callable[..., Response], **kwargs: object) -> None:
        super().__init__(app, **kwargs)
        self._buckets: OrderedDict[str, _TokenBucket] = OrderedDict()

    def _get_bucket(self, key: str) -> "_TokenBucket":
        if key in self._buckets:
            self._buckets.move_to_end(key)
            return self._buckets[key]

        bucket = _TokenBucket(
            rate=RATE_LIMIT_REQUESTS_PER_SECOND,
            capacity=RATE_LIMIT_REQUESTS_PER_SECOND,
        )
        self._buckets[key] = bucket

        while len(self._buckets) > MAX_RATE_LIMIT_ENTRIES:
            self._buckets.popitem(last=False)

        return bucket

    async def dispatch(
        self, request: Request, call_next: Callable[..., Response]
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        bucket = self._get_bucket(client_ip)

        if not bucket.consume():
            logger.warning("rate_limit_exceeded", client_ip=client_ip)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
            )

        return await call_next(request)


class _TokenBucket:
    """Token bucket for rate limiting."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.time()

    def consume(self, tokens: float = 1.0) -> bool:
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity, self._tokens + elapsed * self._rate
        )
        self._last_refill = now

        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False
