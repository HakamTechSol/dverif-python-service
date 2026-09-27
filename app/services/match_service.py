"""
Cross-document matching for the Dvarif document service.

matchDocuments(pathA, pathB, documentTypeA=None, documentTypeB=None):
  - Extract canonical identity fields from each file via the resolved
    document-type schemas (see app.core.document_schemas). `extractText` picks
    the reader from the file's real content type, so a DOCX and a PDF of the
    same document yield the same canonical field map -- the file FORMAT is never
    part of the comparison, only the fields it carries.
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

compareFields(fields_a, fields_b, schema_a, schema_b) is the whole decision and
takes canonical field maps only, so the two sides may come from a live
extraction or from a cache extracted earlier (matchCachedReference). Nothing in
this module ever looks at file bytes: the only byte-level shortcut in the whole
pipeline is the caller-side SHA-256 equality fast path.
"""

import re

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

# Punctuation handling differs per comparison class, and both sides of a
# comparison always get the SAME treatment:
#   * exact fields  -> punctuation is REMOVED, not spaced out. An OCR'd CNIC
#     reads "42101-1234567-1" while a hand-typed one reads "4210112345671";
#     both must normalize to the same token, and inserting a space instead
#     would leave a separator no other value shares.
#   * text fields   -> punctuation becomes a space, so "Muhammad, Ali" and
#     "Muhammad Ali" normalize identically without gluing words together.
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_NON_ALNUM_RE = re.compile(r"[^\w]", re.UNICODE)


def _norm_exact(value: str) -> str:
    """Case-folded, punctuation-free, whitespace-free form for exact fields."""
    return _NON_ALNUM_RE.sub("", str(value).upper())


def _norm_text(value: str) -> str:
    """Case-folded, unpunctuated, whitespace-collapsed form for text fields."""
    text = _NON_WORD_RE.sub(" ", str(value).upper())
    return " ".join(text.split())


# Kept as the historical single entry point; defaults to the text rule, which is
# what every free-text field (name, designation, degree, duration) uses.
def _norm(value: str) -> str:
    return _norm_text(value)


def _field_score(field: str, value_a: str, value_b: str) -> float:
    if field in EXACT_FIELDS:
        return 100.0 if _norm_exact(value_a) == _norm_exact(value_b) else 0.0
    return float(fuzz.ratio(_norm_text(value_a), _norm_text(value_b)))


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


def matchAgainstCachedFields(
    path_a: str,
    cached_fields_b: dict,
    document_type_a: str | None = None,
    document_type_b: str | None = None,
) -> dict:
    """Compare a freshly submitted document against a CACHED reference field map.

    `cached_fields_b` is the canonical field map extracted from an employee
    reference document earlier (at upload time) and cached on its row, in the
    exact shape extractFields() returns. Used so a reference is never re-read
    or re-OCR'd per match, and — because only the extracted fields take part —
    so the reference's file format is irrelevant: a DOCX reference and a PDF
    submission of the same document compare identically on name/CNIC.
    """
    _, schema_a = get_schema(document_type_a)
    _, schema_b = get_schema(document_type_b)

    fields_a = extractFields(extractText(path_a), document_type_a)["fields"]

    return compareFields(fields_a, cached_fields_b, schema_a, schema_b)


def matchCachedAgainstFile(
    cached_fields_a: dict,
    path_b: str,
    document_type_a: str | None = None,
    document_type_b: str | None = None,
) -> dict:
    """Mirror of matchAgainstCachedFields: cached map on side A, live file on B.

    compareFields is symmetric under swapping the two sides, so this is the
    same decision with the arguments exchanged; it exists so either side of a
    /match call can be supplied as a cache.
    """
    _, schema_a = get_schema(document_type_a)
    _, schema_b = get_schema(document_type_b)

    fields_b = extractFields(extractText(path_b), document_type_b)["fields"]

    return compareFields(cached_fields_a, fields_b, schema_a, schema_b)
