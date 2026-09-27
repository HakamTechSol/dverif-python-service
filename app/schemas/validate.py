"""Response models for the /validate endpoint.

The corruption fields (valid, reason, file_type) keep their original names and
types. The crop fields are additive, so older clients that only read the
corruption verdict keep working unchanged.

`check_type` names WHICH TIER produced the verdict, and it is what lets a
client tell an unarguable failure apart from a debatable one:

- ``"structural"`` — the file genuinely cannot be parsed. PyMuPDF cannot open
  the PDF / zero pages / no ``%%EOF``; Pillow ``verify()``+``load()`` fails;
  ``zipfile.testzip()`` finds a bad CRC or required OOXML parts are missing; the
  file is zero-byte or truncated; or the declared extension/mimetype
  contradicts the sniffed content type (MIME spoofing).
  These are hard facts about the bytes. A client should always block these.

- ``"heuristic"`` — a subjective quality signal: crop detection (edge-ink
  density), the darkness/low-contrast readability check, blur, or any similar
  "this looks off" judgement. These are tuning-sensitive and can produce false
  positives on perfectly good scans (a dark scan of a black-background ID card,
  a document photographed on a dark desk, a page that fills the frame on
  purpose). A client may want to surface these for a human to look at rather
  than reject the upload outright.

``check_type`` is always present and always one of those two values. A clean
pass reports ``"structural"``, meaning the hard checks ran and found nothing —
so a client never has to distinguish "no tier" from "structural tier passed".
"""

from typing import Literal

from pydantic import BaseModel

# Which tier of checking produced the verdict.
CheckType = Literal["structural", "heuristic"]


class ValidateData(BaseModel):
    valid: bool
    reason: str | None
    file_type: str | None
    # Always emitted; see the module docstring for the two tiers.
    check_type: CheckType = "structural"
    cropped: bool = False
    crop_reason: str | None = None
    crop_score: float = 0.0


class ValidateResponse(BaseModel):
    success: bool
    data: ValidateData
