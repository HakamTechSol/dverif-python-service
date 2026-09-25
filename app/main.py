"""
Dvarif Document Service — FastAPI microservice.

Purpose: a standalone, independently-run Python process that gives the
Node.js backend three cheap answers: /validate (corrupt-file detection),
/ocr/extract (schema-driven canonical identity-field extraction) and /match
(two-document comparison over those canonical fields).

Run (from this folder):
    uvicorn app.main:app --port 5001

Binds 127.0.0.1 by default (config.settings.host). For a real deployment on
another machine / behind a reverse proxy, opt into all interfaces explicitly:
    set HOST=0.0.0.0  (in .env or the process environment)

Also exposes GET /metrics (Prometheus-style, API-key protected) and writes one
JSON log line per request to stdout.  A Dockerfile is included for container
deployment; DOC_SERVICE_API_KEY must be provided via the environment (the
service is fail-closed: it returns 503 while the key is missing).
"""

import uvicorn
from fastapi import FastAPI

from app.api.routes import health, match, metrics, ocr, validate
from app.core.config import settings
from app.core.logging import init_logger
from app.core.observability import observability
from app.core.security import require_api_key
from app.utils.file_handling import enforce_upload_size_limit

init_logger()

app = FastAPI(
    title="Dvarif Document Service",
    version="0.1.0",
    description="Corrupt-file detection, OCR extraction and document matching for Dvarif.",
)

# Starlette applies middleware in reverse registration order, so the LAST one
# registered is the OUTERMOST. Intent:
#   innermost  enforce_upload_size_limit — 413 for oversized bodies
#              require_api_key           — 401 before any body is buffered
#   outermost  observability            — request id, latency, JSON log, metrics
# All three active middleware layers are wrapped by observability so even the
# 401/413 short-circuits are logged and counted.
app.middleware("http")(enforce_upload_size_limit)
app.middleware("http")(require_api_key)
app.middleware("http")(observability)

app.include_router(health.router)
app.include_router(validate.router)
app.include_router(ocr.router)
app.include_router(match.router)
app.include_router(metrics.router)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)