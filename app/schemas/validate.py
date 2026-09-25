"""Response models for the /validate endpoint.

The corruption fields (valid, reason, file_type) keep their original names and
types. The crop fields are additive, so older clients that only read the
corruption verdict keep working unchanged.
"""

from pydantic import BaseModel


class ValidateData(BaseModel):
    valid: bool
    reason: str | None
    file_type: str | None
    cropped: bool = False
    crop_reason: str | None = None
    crop_score: float = 0.0


class ValidateResponse(BaseModel):
    success: bool
    data: ValidateData