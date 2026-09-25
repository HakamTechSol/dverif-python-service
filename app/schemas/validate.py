"""Response models for the /validate endpoint.

Field order and types mirror exactly what the endpoint previously returned,
so the serialized JSON stays byte-identical.
"""

from pydantic import BaseModel


class ValidateData(BaseModel):
    valid: bool
    reason: str | None
    file_type: str | None


class ValidateResponse(BaseModel):
    success: bool
    data: ValidateData