"""Upload-file helpers for the Dvarif document service.

`_persist_upload` moved from the top level of app.py and hardened with a
per-file size cap. `enforce_upload_size_limit` is an ASGI http middleware
that rejects obviously-oversized requests up front from the Content-Length
header — before the body is ever buffered — while `_persist_upload` ALSO
caps the actual bytes streamed to disk, so a spoofed or absent
Content-Length can never smuggle a file past the limit.
"""

import os

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import settings

# A multipart request body carries the file PLUS boundary/filename headers, so
# the coarse Content-Length pre-check needs a little slack: a file exactly AT
# the cap must never be falsely rejected by the header check alone. The exact
# per-file enforcement happens in `_persist_upload`.
_MULTIPART_SLACK_BYTES = 64 * 1024


def upload_size_limit_bytes() -> int:
    """Per-file upload cap in bytes, from MAX_UPLOAD_SIZE_MB (default 15)."""
    return settings.max_upload_size_mb * 1024 * 1024


def _coarse_allowed_bytes(path: str) -> int:
    """Coarse Content-Length ceiling for the pre-check.

    Per-file cap + multipart slack; doubled for /match, whose request carries
    TWO files (file_a + file_b). This is deliberately generous — the exact
    per-file limit is enforced when the bytes are streamed.
    """
    limit = upload_size_limit_bytes()
    files = 2 if path == "/match" else 1
    return files * limit + _MULTIPART_SLACK_BYTES


async def enforce_upload_size_limit(request: Request, call_next):
    """Reject obviously-oversized uploads from Content-Length before the body
    is read, so a giant body is never buffered into the server's form parser.

    Content-Length is NOT trusted as the final word — it can be spoofed or
    absent — so `_persist_upload` independently caps the streamed bytes.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None
        if declared is not None and declared > _coarse_allowed_bytes(request.url.path):
            return JSONResponse(
                {
                    "success": False,
                    "message": f"Upload exceeds the {settings.max_upload_size_mb} MB limit",
                },
                status_code=413,
                # Do not let uvicorn drain the unread oversized body just to
                # keep the connection reusable; close instead.
                headers={"connection": "close"},
            )
    return await call_next(request)


def _persist_upload(file: UploadFile) -> str:
    """Write the uploaded bytes to a temp file and return its path.

    Streams at most `upload_size_limit_bytes()` per file; a larger file is
    rejected with 413 and the partial temp file is removed.
    """
    import tempfile

    limit = upload_size_limit_bytes()
    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)

    try:
        with os.fdopen(fd, "wb") as out:
            total = 0
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds the {settings.max_upload_size_mb} MB limit",
                    )
                out.write(chunk)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return tmp_path