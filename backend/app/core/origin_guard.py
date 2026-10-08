"""Origin-verification middleware for FastAPI.

CloudFront adds a secret ``X-Origin-Verify`` header to every request it
forwards.  This middleware rejects any request that arrives without that header
(or with the wrong value), preventing callers from bypassing CloudFront and
hitting the service directly.

The check is skipped in two situations:

1. **Local development** – when ``Settings.origin_verify_secret`` is empty the
   middleware allows all requests through.  In local ``docker-compose`` the
   secret is not set, so developers can call the API directly.

2. **Health endpoints** – ``/healthz`` and ``/readyz`` are always allowed so
   that load-balancers, ECS health checks, and Lambda invoke probes can reach
   them without the CloudFront header.

Usage::

    from app.core.origin_guard import OriginGuardMiddleware
    app.add_middleware(OriginGuardMiddleware)
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings

# Paths that are always allowed through regardless of the header.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz"})

_FORBIDDEN_BODY = {"error": {"code": "FORBIDDEN", "message": "Forbidden"}}


class OriginGuardMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces the CloudFront ``X-Origin-Verify`` header.

    Install it with::

        app.add_middleware(OriginGuardMiddleware)

    The middleware reads the secret from ``get_settings()`` on every request so
    that tests can clear the settings cache and inject different values without
    restarting the process.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Health endpoints are always exempt.
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        settings = get_settings()

        # Empty secret → local development mode; allow everything.
        if not settings.origin_verify_secret:
            return await call_next(request)

        # Verify the header.
        header_value = request.headers.get("X-Origin-Verify", "")
        if header_value != settings.origin_verify_secret:
            return JSONResponse(status_code=403, content=_FORBIDDEN_BODY)

        return await call_next(request)
