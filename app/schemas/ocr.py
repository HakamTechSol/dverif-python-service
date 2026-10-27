"""Response models for the /ocr/extract endpoint.

Serializer shapes move from the old flat {name, cnic} to a canonical,
per-document-type field map so the Node backend can compare structured fields
instead of raw OCR text.
"""

from typing import Any, Literal

from pydantic import BaseModel, model_serializer


class ExtractedField(BaseModel):
    value: str | None
    confidence: Literal["high", "low", "not_visible"]


class LayaAnswer(BaseModel):
    """One normalised Laya answer.

    `value` is a bool for a `noul` question and a string label for a `choice`
    one. `advisory_only` marks the questions that are NOT calibrated for HR
    documents (see app/services/laya_engine.py for the measurements) — callers
    must not reject or approve a document on those.
    """

    type: str | None
    value: Any
    probability: float
    probabilities: dict[str, float]
    confidence: float
    advisory_only: bool


class LayaResult(BaseModel):
    """Advisory block attached to an /ocr/extract response.

    `available: false` carries a machine-readable `reason` instead of answers:
    disabled, empty_text, router_unavailable, busy, timeout or
    inference_failed. It is never an error — the extracted fields beside it are
    the authoritative verification signal.
    """

    available: bool
    reason: str | None = None
    skipped: bool | None = None
    detail: str | None = None
    timeout_s: float | None = None
    model: str | None = None
    checkpoint: str | None = None
    routed_by: str | None = None
    elapsed_ms: float | None = None
    truncated: bool | None = None
    answers: dict[str, LayaAnswer] | None = None
    usage: dict[str, Any] | None = None

    @model_serializer
    def _drop_unset(self) -> dict[str, Any]:
        """Emit only the keys that apply.

        An unavailable engine would otherwise serialise nine nulls next to
        `available: false`. Deliberately NOT `exclude_none=True` on the route's
        response_model: `ExtractedField.value` is legitimately None for a
        "not_visible" field, and that null carries meaning.
        """
        return {k: v for k, v in self.__dict__.items() if v is not None}


class OcrData(BaseModel):
    document_type: str
    fields: dict[str, ExtractedField]
    laya: LayaResult | None = None


class OcrResponse(BaseModel):
    success: bool
    data: OcrData