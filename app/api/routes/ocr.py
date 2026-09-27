"""POST /ocr/extract — route handler only. Business logic lives in the services layer."""

import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.schemas.ocr import OcrResponse
from app.services.ocr_service import extractFields, extractText
from app.utils.file_handling import _persist_upload

router = APIRouter()


@router.post("/ocr/extract", response_model=OcrResponse)
def run_ocr_extract(
    request: Request,
    file: UploadFile = File(...),
    document_type: str | None = Form(None),
):
    """Extract the canonical identity fields buried inside a document.

    Accepts a multipart upload named ``file`` and an optional
    ``document_type`` form field that selects the field schema (see
    app.core.document_schemas; omitted/unknown types use the generic schema).
    The reader is chosen from the file's real content type, not its extension:
    a PDF is rendered and run through Tesseract, a raster image is OCR'd
    directly, and a DOCX — which is a live document, not a scan — has its text
    read straight out of word/document.xml with python-docx. All three then feed
    the SAME canonical extractor, which is why a DOCX reference and a PDF
    submission of one document produce comparable field maps.

    This is the single extraction entry point: the backend calls it for a
    verification-request upload AND, at upload time, for an employee reference
    document whose fields it then caches for later matching. Returns
    ``{"success", "data": {document_type, fields}}`` where every field carries
    {value, confidence} and unextracted required fields are marked
    "not_visible".
    """

    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required")

    tmp_path = None
    try:
        tmp_path = _persist_upload(file)
        text = extractText(tmp_path)
        result = extractFields(text, document_type)
        # Observability metadata only — never the extracted values.
        request.state.document_type = result["document_type"]
        request.state.outcome = "ok"
        return {"success": True, "data": result}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)