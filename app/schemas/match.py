"""Response models for the /match endpoint.

Field order and types mirror exactly what the endpoint previously returned,
so the serialized JSON stays byte-identical.
"""

from pydantic import BaseModel


class MatchData(BaseModel):
    match: bool
    confidence: float
    reasons: list[str]


class MatchResponse(BaseModel):
    success: bool
    data: MatchData