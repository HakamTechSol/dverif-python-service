"""Response models for the /match endpoint.

`match`, `confidence` and `reasons` keep their original meaning and types, so
every existing consumer keeps working unchanged.

Two flags are added on top. Both default to the permissive value, which means an
older service build that does not send them is treated as "no problem found"
rather than as a hard failure — a conservative default for a field the caller
may simply not have populated.

  identity_mismatch    The CNIC comparison did not establish that the two
                       documents describe the same person: the two CNICs differ,
                       or one document shows a CNIC and the other does not. This
                       is a FACT about the comparison, reported even when the
                       score is high, because a high average across many
                       non-identity fields can otherwise bury a single field
                       that says the documents are about different people.

  auto_match_eligible  The single question a caller actually has to act on:
                       may this result be auto-approved? It is False for an
                       identity-poor document type (photo, NDA, policy
                       acknowledgment, handover form, ...) and False whenever
                       `identity_mismatch` is set. Derived from the flag above
                       plus the type gate, so a caller can honour one boolean
                       instead of re-deriving the policy.
"""


from pydantic import BaseModel


class MatchData(BaseModel):
    match: bool
    confidence: float
    reasons: list[str]
    identity_mismatch: bool = False
    auto_match_eligible: bool = True


class MatchResponse(BaseModel):
    success: bool
    data: MatchData
