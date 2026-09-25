"""
Cross-document matching for the Dvarif document service.

matchDocuments(pathA, pathB, documentTypeA=None, documentTypeB=None):
  - OCR both files and extract ONLY canonical identity fields via the resolved
    document-type schemas (see app.core.document_schemas).
  - Compare each canonical field that BOTH documents carry:
      * exact fields (cnic, dates, account numbers, ...) match only when equal;
      * text fields (name, designation, degree, ...) use rapidfuzz
        (ratio >= 88 counts as a match).
  - Hard-fail rules (a required field is one BOTH schemas mark required):
      * a required field that is not visible on one/both documents,
      * a required field whose values differ (a low-confidence extraction on
        either side is noted; it cannot rescue a required mismatch).
  - Optional fields absent on one side are skipped, never failing the match.
  - Confidence = average of the comparable field scores (0-100 each).
"""

from rapidfuzz import fuzz

from app.core.document_schemas import get_schema
from app.services.ocr_service import extractFields, extractText

NAME_MATCH_THRESHOLD = 88
MATCH_CONFIDENCE_THRESHOLD = 75.0

# Fields compared by exact equality after normalization.
EXACT_FIELDS = {
    "cnic",
    "dob", "joining_date", "effective_date", "resignation_date", "recommendation_date",
    "session", "year", "increment", "bank_account", "passport_no", "tax_year",
}

# Fields compared with fuzzy string similarity.
FUZZY_FIELDS = {"name", "designation", "degree", "duration"}


def _norm(value: str) -> str:
    return " ".join(str(value).upper().split())


def _field_score(field: str, value_a: str, value_b: str) -> float:
    if field in EXACT_FIELDS:
        return 100.0 if _norm(value_a) == _norm(value_b) else 0.0
    return float(fuzz.ratio(_norm(value_a), _norm(value_b)))


def compareFields(
    fields_a: dict,
    fields_b: dict,
    schema_a: dict,
    schema_b: dict,
) -> dict:
    """Compare two canonical field maps and return {match, confidence, reasons}.

    Pure function (no I/O), so it is unit-testable without running OCR.
    fields_a/fields_b are the "fields" dicts returned by extractFields();
    schema_a/schema_b come from app.core.document_schemas.get_schema().
    """
    common = [field for field in fields_a if field in fields_b]

    reasons: list[str] = []
    scores: list[float] = []
    hard_fail = False

    for field in common:
        required = bool(schema_a.get(field, False)) and bool(schema_b.get(field, False))
        a, b = fields_a[field], fields_b[field]
        value_a = (a or {}).get("value")
        value_b = (b or {}).get("value")

        if not value_a or not value_b:
            if required:
                hard_fail = True
                reasons.append(f"{field}: required but not visible on both documents")
            continue

        score = _field_score(field, str(value_a), str(value_b))
        scores.append(score)

        if score >= NAME_MATCH_THRESHOLD:
            reasons.append(f"{field} matches ({score:.0f}%)")
        elif required:
            hard_fail = True
            conf_a = (a or {}).get("confidence")
            conf_b = (b or {}).get("confidence")
            low = conf_a == "low" or conf_b == "low"
            reasons.append(
                f"{field} differs ({score:.0f}%)"
                + (" with a low-confidence extraction" if low else "")
            )
        else:
            reasons.append(f"{field} differs ({score:.0f}%)")

    if hard_fail:
        return {
            "match": False,
            "confidence": 0.0,
            "reasons": reasons or ["Required identity fields could not be compared"],
        }

    if not scores:
        return {
            "match": False,
            "confidence": 0.0,
            "reasons": ["No comparable identity fields could be extracted from both documents"],
        }

    confidence = sum(scores) / len(scores)
    return {
        "match": confidence >= MATCH_CONFIDENCE_THRESHOLD,
        "confidence": round(confidence, 2),
        "reasons": reasons,
    }


def matchDocuments(
    path_a: str,
    path_b: str,
    document_type_a: str | None = None,
    document_type_b: str | None = None,
) -> dict:
    """Compare two documents. Returns {match, confidence, reasons}."""
    _, schema_a = get_schema(document_type_a)
    _, schema_b = get_schema(document_type_b)

    fields_a = extractFields(extractText(path_a), document_type_a)["fields"]
    fields_b = extractFields(extractText(path_b), document_type_b)["fields"]

    return compareFields(fields_a, fields_b, schema_a, schema_b)