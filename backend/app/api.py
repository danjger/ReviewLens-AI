"""API service – FastAPI application entry point.

The API app mounts every HTTP route under the ``/api`` prefix (repo convention),
registers the shared ``{"error": {"code", "message"}}`` error handlers, and
installs the CloudFront origin-verification middleware. Domain routers are
included here; the ingestion Check endpoints (dataset-ingestion task 4.3) are
the first to land.
"""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.chat.history_api import router as chat_history_router
from app.chat.save_api import router as chat_save_router
from app.chat.suggestions_api import router as chat_suggestions_router
from app.core.errors import register_error_handlers
from app.core.health import liveness, readiness
from app.core.origin_guard import OriginGuardMiddleware
from app.datasets.api import router as library_router
from app.datasets.summary_api import router as summary_router
from app.ingestion.api import datasets_router, register_rate_limit_header, uploads_router
from app.ingestion.api import router as ingestion_router

app = FastAPI(title="ReviewLens API")

# CloudFront origin verification: reject requests that bypass CloudFront.
# A no-op in local development where the secret is empty.
app.add_middleware(OriginGuardMiddleware)

# Shared error envelope for AppError / validation / unexpected errors.
register_error_handlers(app)
# Add the Retry-After header to rate-limit (429) responses.
register_rate_limit_header(app)

# Domain routers (all under the /api prefix per repo conventions).
app.include_router(ingestion_router, prefix="/api")
app.include_router(uploads_router, prefix="/api")
# The ingestion-owned ``POST /datasets/upload`` router is included before the
# Library router so its fixed ``/upload`` path is matched ahead of the Library's
# ``GET /datasets/{dataset_id}`` path parameter.
app.include_router(datasets_router, prefix="/api")
# Ingestion Summary endpoints (snapshot URL, reviews). Included before the
# Library router so its two-segment ``/datasets/{id}/snapshot-url`` path is
# registered alongside the Library's ``/datasets/{id}`` routes.
app.include_router(summary_router, prefix="/api")
# Chat history (guardrailed-chat task 5.1). The streaming chat POST lives on the
# chat service; history is an ordinary API-service route (design "Endpoints").
# Registered before the Library router so its three-segment
# ``/datasets/{id}/chat/history`` path is matched alongside the Library's
# ``/datasets/{id}`` routes.
app.include_router(chat_history_router, prefix="/api")
# Chat retry-save (guardrailed-chat task 5.2). Like history, an ordinary
# API-service route (design "Endpoints"). Its three-segment
# ``/datasets/{id}/chat/save`` path is registered before the Library router so
# it is matched alongside the Library's ``/datasets/{id}`` routes.
app.include_router(chat_save_router, prefix="/api")
# Chat suggestions (guardrailed-chat task 5.3). Like history and save, an
# ordinary API-service route (design "Endpoints"). Its three-segment
# ``/datasets/{id}/chat/suggestions`` path is registered before the Library
# router so it is matched alongside the Library's ``/datasets/{id}`` routes.
app.include_router(chat_suggestions_router, prefix="/api")
app.include_router(library_router, prefix="/api")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness probe – returns 200 if the process is alive."""
    return liveness()


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness probe – checks database and S3 connectivity."""
    return readiness(
        database_url=os.environ.get("DATABASE_URL"),
        s3_bucket=os.environ.get("S3_BUCKET"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    )
