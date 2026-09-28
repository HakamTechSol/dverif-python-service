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
   - Optional fields absent on one side are skipped, never failing the match --
       EXCEPT the always-compared identity fields (ALWAYS_COMPARE_FIELDS), which
       are never skipped. A CNIC that one document shows and the other does not
       is scored 0 and raises `identity_mismatch`, instead of being dropped and
       letting the remaining fields report a clean 100.
   - Confidence = average of the comparable field scores (0-100 each).

Two flags travel with every result, because a score alone is not a safe thing to
auto-approve on:
   * `identity_mismatch`  - the CNIC comparison did not establish one person.
   * `auto_match_eligible`- may this be auto-approved at all. False for an
     identity-poor document type, and False whenever `identity_mismatch` is set.
     Callers must honour this rather than re-deriving the threshold: a single
     mismatched identity field can be averaged away by a document type that
     carries many matching non-identity fields.

compareFields(fields_a, fields_b, schema_a, schema_b) is the whole decision and
takes canonical field maps only, so the two sides may come from a live
extraction or from a cache extracted earlier (matchCachedReference). Nothing in
this module ever looks at file bytes: the only byte-level shortcut in the whole
pipeline is the caller-side SHA-256 equality fast path.
"""

import re

from rapidfuzz import fuzz

from app.core.document_schemas import (
    ALWAYS_COMPARE_FIELDS,
    get_schema,
    is_auto_match_eligible,
)
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


def _result(
    match: bool,
    confidence: float,
    reasons: list[str],
    identity_mismatch: bool = False,
) -> dict:
    """Build a /match result payload.

    Two things are forced here rather than left to each caller.

    `match` is downgraded to False whenever `identity_mismatch` is set. The
    confidence figure is still reported unchanged, but `match` must never
    contradict the identity verdict: a document type whose other fields happen
    to all agree averages above MATCH_CONFIDENCE_THRESHOLD even with the CNIC
    scoring 0 (a letter matching on name + designation + joining_date averages
    exactly 75.0), so a consumer reading only `match` would approve a document
    whose own CNIC says it belongs to somebody else. `match: true` sitting
    next to `identity_mismatch: true` is exactly the quiet wrong answer this
    service is supposed to never produce.

    `auto_match_eligible` is derived, not passed in: it is False whenever the
    identity comparison failed, so a caller can act on one boolean without
    re-deriving the policy (the per-type gate is applied separately, before
    extraction, in the match* entry points).
    """
    return {
        "match": False if identity_mismatch else match,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "identity_mismatch": identity_mismatch,
        "auto_match_eligible": not identity_mismatch,
    }


def compareFields(
    fields_a: dict,
    fields_b: dict,
    schema_a: dict,
    schema_b: dict,
) -> dict:
    """Compare two canonical field maps and return the /match payload.

    Pure function (no I/O), so it is unit-testable without running OCR.
    fields_a/fields_b are the "fields" dicts returned by extractFields();
    schema_a/schema_b come from app.core.document_schemas.get_schema().

    Returns {match, confidence, reasons, identity_mismatch, auto_match_eligible}.
    """
    # The fields actually compared: those BOTH sides carry, PLUS every
    # always-compared field carried by EITHER side.
    #
    # Including the one-sided identity fields is the whole point. Restricting to
    # the intersection means a field missing from one map is dropped without a
    # trace, which is precisely how a CNIC-copy submission came to match an
    # employment reference at 100.0: the reference map had no `cnic` key at all
    # (a cached extraction taken before every schema declared one), so the
    # intersection held only `name`. A missing side is now read as "not visible"
    # and scored as unconfirmed identity rather than skipped.
    common = [field for field in fields_a if field in fields_b]
    for field in ALWAYS_COMPARE_FIELDS:
        if field not in common and (field in fields_a or field in fields_b):
            common.append(field)

    reasons: list[str] = []
    scores: list[float] = []
    hard_fail = False
    identity_mismatch = False

    for field in common:
        required = bool(schema_a.get(field, False)) and bool(schema_b.get(field, False))
        always_compare = field in ALWAYS_COMPARE_FIELDS
        a, b = fields_a.get(field), fields_b.get(field)
        value_a = (a or {}).get("value")
        value_b = (b or {}).get("value")

        if not value_a or not value_b:
            if required:
                hard_fail = True
                # A required identity field that is not on both sides is an
                # identity problem as well as a visibility one, so the flag is
                # raised even though the hard-fail already forces a 0.0. The
                # caller can then report one consistent reason.
                if always_compare:
                    identity_mismatch = True
                reasons.append(f"{field}: required but not visible on both documents")
            elif always_compare and bool(value_a) != bool(value_b):
                # An identity field on ONE side only. This is NOT the ordinary
                # "optional field absent" case: the document says something the
                # other one does not, and the two cannot be shown to describe
                # the same person. Scoring it 0 drags the confidence down and the
                # flag makes the verdict unconditional, because a high average
                # over the remaining fields would otherwise bury it.
                scores.append(0.0)
                identity_mismatch = True
                shown_on = "submitted document" if value_a else "reference document"
                reasons.append(
                    f"{field} appears on the {shown_on} but not on the other — "
                    "identity could not be confirmed (0%)"
                )
            continue

        score = _field_score(field, str(value_a), str(value_b))
        scores.append(score)

        if always_compare and score < NAME_MATCH_THRESHOLD:
            # Two different identifiers. The score is already 0 for this field,
            # but the flag is what makes the outcome safe: on a document type
            # with many comparable fields, one 0 does not by itself prevent an
            # average that clears the auto-approve bar.
            identity_mismatch = True

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
        return _result(
            False,
            0.0,
            reasons or ["Required identity fields could not be compared"],
            identity_mismatch,
        )

    if not scores:
        return _result(
            False,
            0.0,
            ["No comparable identity fields could be extracted from both documents"],
            identity_mismatch,
        )

    confidence = sum(scores) / len(scores)
    return _result(
        confidence >= MATCH_CONFIDENCE_THRESHOLD,
        confidence,
        reasons,
        identity_mismatch,
    )


def _ineligible_result(reason: str) -> dict:
    """The verdict for a document type that can never establish identity."""
    return {
        "match": False,
        "confidence": 0.0,
        "reasons": [reason],
        "identity_mismatch": False,
        "auto_match_eligible": False,
    }


def _gate_on_document_type(*document_types: str | None) -> dict | None:
    """Refuse the comparison when a side's document type cannot establish identity.

    Evaluated BEFORE any extraction, so an identity-poor type costs nothing to
    decline: no OCR is run and no score is produced. Returns a result dict to
    return early, or None when the comparison may proceed.

    Both sides are checked. A submitted photograph must not be auto-approved
    against an employment reference any more than the reverse.
    """
    for document_type in document_types:
        eligible, reason = is_auto_match_eligible(document_type)
        if not eligible:
            return _ineligible_result(reason)
    return None


def matchDocuments(
    path_a: str,
    path_b: str,
    document_type_a: str | None = None,
    document_type_b: str | None = None,
) -> dict:
    """Compare two documents. Returns the /match payload."""
    blocked = _gate_on_document_type(document_type_a, document_type_b)
    if blocked is not None:
        return blocked

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
    blocked = _gate_on_document_type(document_type_a, document_type_b)
    if blocked is not None:
        return blocked

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
    blocked = _gate_on_document_type(document_type_a, document_type_b)
    if blocked is not None:
        return blocked

    _, schema_a = get_schema(document_type_a)
    _, schema_b = get_schema(document_type_b)

    fields_b = extractFields(extractText(path_b), document_type_b)["fields"]

    return compareFields(cached_fields_a, fields_b, schema_a, schema_b)
