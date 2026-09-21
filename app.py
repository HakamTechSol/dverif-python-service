"""
Dvarif Document Service — FastAPI microservice.

Purpose: a standalone, independently-run Python process that gives the
Node.js backend three cheap answers: /validate (corrupt-file detection),
/ocr/extract (name + CNIC extraction) and /match (two-document comparison).

Run (from this folder):
    uvicorn app:app --port 5001
"""

import os

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from validate import validate as validate_file
from ocr import extractCnicAndName, extractText
from match import matchDocuments


class Settings(BaseSettings):
    """Reads PORT and DOC_SERVICE_API_KEY from the .env file in this folder."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = 5001
    doc_service_api_key: str = ""


settings = Settings()

app = FastAPI(
    title="Dvarif Document Service",
    version="0.1.0",
    description="Corrupt-file detection, OCR extraction and document matching for Dvarif.",
)


@app.middleware("http")
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
        )
    if key != expected:
        return JSONResponse(
            {"success": False, "message": "Invalid or missing API key"},
            status_code=401,
        )
    return await call_next(request)


def validate(file_path: str, filename: str) -> dict:
    """Corrupt-file detection: magic-byte sniff + structural parse."""
    return validate_file(file_path, filename)


@app.get("/health")
async def health():
    """Liveness probe — no API key required."""
    return {"status": "ok"}


@app.post("/validate")
async def run_validate(file: UploadFile = File(...)):
    """Validate an uploaded file for corruption.

    Accepts a multipart upload named ``file``. Checks magic bytes against
    the declared type, then structurally opens the file (PDF via PyMuPDF,
    images via Pillow, ZIP/DOCX/XLSX via zipfile) so truncation and CRC
    failures are caught. Returns {"success", "data": {valid, reason, file_type}}.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required")

    tmp_path = None
    try:
        tmp_path = _persist_upload(file)
        result = validate(tmp_path, file.filename)
        return {"success": True, "data": result}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/ocr/extract")
def run_ocr_extract(file: UploadFile = File(...)):
    """Extract the identity fields (name, CNIC) buried inside a document.

    Accepts a multipart upload named ``file`` (PDF or image). Renders the
    PDF to an image and runs Tesseract, then pulls out the CNIC via regex
    and the name via a "Name" label heuristic. Returns
    {"success", "data": {name, cnic}}.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required")

    tmp_path = None
    try:
        tmp_path = _persist_upload(file)
        text = extractText(tmp_path)
        name, cnic = extractCnicAndName(text)
        return {"success": True, "data": {"name": name, "cnic": cnic}}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/match")
def run_match(file_a: UploadFile = File(...), file_b: UploadFile = File(...)):
    """Compare two documents to see whether they belong to the same person.

    Accepts multipart uploads named ``file_a`` and ``file_b``. OCRs both,
    fuzzy-matches the names via rapidfuzz and exactly compares normalized
    CNICs, then returns {"success", "data": {match, confidence, reasons}}.
    """
    if not file_a.filename or not file_b.filename:
        raise HTTPException(status_code=400, detail="Both file_a and file_b are required")

    tmp_a = tmp_b = None
    try:
        tmp_a = _persist_upload(file_a)
        tmp_b = _persist_upload(file_b)
        result = matchDocuments(tmp_a, tmp_b)
        return {"success": True, "data": result}
    finally:
        for tmp in (tmp_a, tmp_b):
            if tmp and os.path.exists(tmp):
                os.remove(tmp)


def _persist_upload(file: UploadFile) -> str:
    """Write the uploaded bytes to a temp file and return its path."""
    import tempfile

    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    return tmp_path


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=settings.port, reload=True)