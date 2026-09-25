"""API-key verification for the Dvarif document service.

Moved from the top-level middleware in app.py; the key comparison now uses
hmac.compare_digest so timing side-channels cannot be used to brute-force
the key over the network.
"""

import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import settings


async def require_api_key(request: Request, call_next):
    """Reject every request that is not /health unless X-API-Key matches.

    The single exception is /health, which stays open so uptime monitors
    (and the Node.js backend) can reach it without credentials.
    """
    if request.url.path == "/health":
        return await call_next(request)

    key = request.headers.get("x-api-key", "")
    expected = settings.doc_service_api_key or os.getenv("DOC_SERVICE_API_KEY", "")

    if not expected:
        return JSONResponse(
            {"success": False, "message": "Document service is not configured (missing DOC_SERVICE_API_KEY)"},
            status_code=503,
            # Rejecting without draining the (possibly oversized) uploaded body.
            headers={"connection": "close"},
        )

    # Constant-time comparison: `==` can be brute-forced character-by-character
    # through response-time side channels.
    if not hmac.compare_digest(key, expected):
        return JSONResponse(
            {"success": False, "message": "Invalid or missing API key"},
            status_code=401,
            headers={"connection": "close"},
        )
    return await call_next(request)