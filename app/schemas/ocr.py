"""Response models for the /ocr/extract endpoint.

Serializer shapes move from the old flat {name, cnic} to a canonical,
per-document-type field map so the Node backend can compare structured fields
instead of raw OCR text.
"""

from typing import Literal

from pydantic import BaseModel


class ExtractedField(BaseModel):
    value: str | None
    confidence: Literal["high", "low", "not_visible"]


class OcrData(BaseModel):
    document_type: str
    fields: dict[str, ExtractedField]


class OcrResponse(BaseModel):
    success: bool
    data: OcrData