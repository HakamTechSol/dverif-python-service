"""POST /validate — route handler only. Business logic lives in the services layer."""

import os

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.schemas.validate import ValidateResponse
from app.services.validation_service import validate as validate_file
from app.utils.file_handling import _persist_upload

router = APIRouter()


@router.post("/validate", response_model=ValidateResponse)
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
        result = validate_file(tmp_path, file.filename)
        return {"success": True, "data": result}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)