"""
Cross-document matching for the Dvarif document service.

matchDocuments(pathA, pathB):
  - OCR both files, extract (name, cnic) from each.
  - Name: fuzzy comparison via rapidfuzz (ratio >= 88% counts as a match).
  - CNIC: exact comparison after normalization (13 digits, stripped).
  - Confidence = 0.6 * name_score + 0.4 * cnic_score (0-100 each).
  - match=True when confidence >= 75.
"""

from rapidfuzz import fuzz

from ocr import extractCnicAndName, extractText

NAME_MATCH_THRESHOLD = 88
MATCH_CONFIDENCE_THRESHOLD = 75.0

# name/CNIC contribution weights for the final confidence score
NAME_WEIGHT = 0.6
CNIC_WEIGHT = 0.4


def _name_score(name_a: str | None, name_b: str | None) -> float | None:
    if not name_a or not name_b:
        return None
    return float(fuzz.ratio(name_a.upper(), name_b.upper()))


def _cnic_score(cnic_a: str | None, cnic_b: str | None) -> float | None:
    if not cnic_a or not cnic_b:
        return None
    return 100.0 if cnic_a == cnic_b else 0.0


def matchDocuments(path_a: str, path_b: str) -> dict:
    """Compare two documents. Returns {match, confidence, reasons}."""
    text_a = extractText(path_a)
    text_b = extractText(path_b)

    name_a, cnic_a = extractCnicAndName(text_a)
    name_b, cnic_b = extractCnicAndName(text_b)

    name_score = _name_score(name_a, name_b)
    cnic_score = _cnic_score(cnic_a, cnic_b)

    reasons: list[str] = []
    if name_score is None:
        reasons.append("Name could not be extracted from both documents")
    elif name_score >= NAME_MATCH_THRESHOLD:
        reasons.append(f"Name matches ({name_score:.0f}%)")
    else:
        reasons.append(f"Name differs ({name_score:.0f}%)")

    if cnic_score is None:
        reasons.append("CNIC could not be extracted from both documents")
    elif cnic_score == 100.0:
        reasons.append("CNIC matches")
    else:
        reasons.append("CNIC differs")

    confidence = (
        NAME_WEIGHT * (name_score or 0.0)
        + CNIC_WEIGHT * (cnic_score or 0.0)
    )

    return {
        "match": confidence >= MATCH_CONFIDENCE_THRESHOLD,
        "confidence": round(confidence, 2),
        "reasons": reasons,
    }