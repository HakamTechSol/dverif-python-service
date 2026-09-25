"""Request observability: JSON log line + metrics tallies per request.

This middleware is the OUTERMOST one: it assigns a request id, times every
request (including /health and the 401/413 short-circuits produced by the
inner API-key and upload-size middlewares), writes a single structured JSON
log line and updates the in-memory metrics registry.

Privacy contract: this code only ever reads request metadata from
``request.state`` (document_type / outcome, set by the route handlers). The
extracted name / CNIC values and raw OCR text are never passed in here.
"""

import time
import uuid

from fastapi import Request

from app.core import metrics
from app.core.logging import get_logger


async def observability(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id

    logger = get_logger()
    start = time.perf_counter()
    status = 500
    completed = False

    try:
        response = await call_next(request)
        status = response.status_code
        completed = True
    except Exception:
        logger.error(
            "http_error",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            },
        )
        raise
    finally:
        duration_s = time.perf_counter() - start
        path = request.url.path
        document_type = getattr(request.state, "document_type", None)
        outcome = getattr(request.state, "outcome", None)

        metrics.record_request(path, status, duration_s)
        if document_type and outcome:
            metrics.record_outcome(path, document_type, outcome)

        if completed:
            logger.info(
                "http_request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "duration_ms": round(duration_s * 1000, 2),
                    "document_type": document_type,
                    "outcome": outcome,
                },
            )

    response.headers["X-Request-Id"] = request_id
    return response