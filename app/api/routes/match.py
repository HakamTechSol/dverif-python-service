"""POST /match — route handler only. Business logic lives in the services layer."""

import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.core.document_schemas import resolve_document_type
from app.schemas.match import MatchResponse
from app.services.match_service import (
    matchAgainstCachedFields,
    matchCachedAgainstFile,
    matchDocuments,
)
from app.utils.file_handling import _persist_upload

router = APIRouter()

# Cap on the canonical field map accepted from a caller, so a cached payload
# cannot be used to push an arbitrarily large document through the service.
MAX_CACHED_FIELDS_BYTES = 64 * 1024


def _parse_cached_fields(raw: str | None, field_name: str) -> dict | None:
    """Decode a cached canonical field map sent instead of a file.

    Shape: {"fields": {"name": {"value": ..., "confidence": ...}, ...}}.
    Returns None when absent. Raises 400 on malformed input — a silently
    ignored cache would fall through to a comparison the caller did not ask
    for, which is exactly the kind of quiet wrong answer this path must not
    produce.
    """
    if raw is None or not raw.strip():
        return None
    if len(raw.encode("utf-8")) > MAX_CACHED_FIELDS_BYTES:
        raise HTTPException(status_code=413, detail=f"{field_name} is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON object")

    fields = payload.get("fields", payload)
    if not isinstance(fields, dict) or not fields:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must carry a non-empty 'fields' object"
        )
    return fields


@router.post("/match", response_model=MatchResponse)
def run_match(
    request: Request,
    file_a: UploadFile | None = File(None),
    file_b: UploadFile | None = File(None),
    document_type_a: str | None = Form(None),
    document_type_b: str | None = Form(None),
    file_a_fields: str | None = Form(None),
    file_b_fields: str | None = Form(None),
):
    """Compare two documents to see whether they belong to the same person.

    Accepts multipart uploads named ``file_a`` and ``file_b`` plus optional
    ``document_type_a`` / ``document_type_b`` form fields that select each
    file's field schema. Each side may instead be supplied as a pre-extracted
    canonical field map (``file_a_fields`` / ``file_b_fields``), which is how a
    reference document extracted once at upload time is compared without being
    re-read. A side given as cached fields needs no file at all.

    Only canonical identity fields take part in the decision: the files' formats
    and bytes are irrelevant beyond the caller-side SHA-256 fast path, so a DOCX
    reference and a PDF submission of the same document match on content.
    Returns ``{"success", "data": {match, confidence, reasons}}``.
    """
    cached_a = _parse_cached_fields(file_a_fields, "file_a_fields")
    cached_b = _parse_cached_fields(file_b_fields, "file_b_fields")

    if cached_a is None and (file_a is None or not file_a.filename):
        raise HTTPException(status_code=400, detail="file_a (or file_a_fields) is required")
    if cached_b is None and (file_b is None or not file_b.filename):
        raise HTTPException(status_code=400, detail="file_b (or file_b_fields) is required")

    tmp_a = tmp_b = None
    try:
        if cached_a is None:
            tmp_a = _persist_upload(file_a)
        if cached_b is None:
            tmp_b = _persist_upload(file_b)

        if cached_b is not None:
            result = matchAgainstCachedFields(tmp_a, cached_b, document_type_a, document_type_b)
        elif cached_a is not None:
            result = matchCachedAgainstFile(cached_a, tmp_b, document_type_a, document_type_b)
        else:
            result = matchDocuments(tmp_a, tmp_b, document_type_a, document_type_b)

        # Observability metadata only — never the extracted values.
        request.state.document_type = resolve_document_type(document_type_a or document_type_b)
        request.state.outcome = "match" if result["match"] else "no_match"
        return {"success": True, "data": result}
    finally:
        for tmp in (tmp_a, tmp_b):
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
