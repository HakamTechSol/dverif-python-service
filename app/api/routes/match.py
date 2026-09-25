"""POST /match — route handler only. Business logic lives in the services layer."""

import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.core.document_schemas import resolve_document_type
from app.schemas.match import MatchResponse
from app.services.match_service import matchDocuments
from app.utils.file_handling import _persist_upload

router = APIRouter()


@router.post("/match", response_model=MatchResponse)
def run_match(
    request: Request,
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    document_type_a: str | None = Form(None),
    document_type_b: str | None = Form(None),
):
    """Compare two documents to see whether they belong to the same person.

    Accepts multipart uploads named ``file_a`` and ``file_b`` plus optional
    ``document_type_a`` / ``document_type_b`` form fields that select each
    file's field schema. OCRs both, extracts ONLY canonical identity fields,
    compares them (skipping optional fields absent on one side, failing hard on
    required-field mismatches), then returns
    {"success", "data": {match, confidence, reasons}}.
    """
    if not file_a.filename or not file_b.filename:
        raise HTTPException(status_code=400, detail="Both file_a and file_b are required")

    tmp_a = tmp_b = None
    try:
        tmp_a = _persist_upload(file_a)
        tmp_b = _persist_upload(file_b)
        result = matchDocuments(tmp_a, tmp_b, document_type_a, document_type_b)
        # Observability metadata only — never the extracted values.
        request.state.document_type = resolve_document_type(document_type_a)
        request.state.outcome = "match" if result["match"] else "no_match"
        return {"success": True, "data": result}
    finally:
        for tmp in (tmp_a, tmp_b):
            if tmp and os.path.exists(tmp):
                os.remove(tmp)