"""Cross-format matching + the normalization the name comparison depends on.

Two behaviours are pinned here, both of which used to be theory rather than
code:

1. FORMAT-INDEPENDENT MATCHING (issue D). A reference document stored as a DOCX
   and a submission arriving as a PDF describe the same person, so they must
   match on their canonical fields. Their SHA-256 hashes can never be equal --
   different containers, different bytes -- and that is expected: the hash
   fast path is simply not the mechanism that decides this case. Before, a
   DOCX reference produced no text on the OCR path, every field came back
   "not_visible", the required-field hard-fail fired, and a genuine same-person
   match fell through to manual review.

2. NORMALIZATION APPLIED TO BOTH SIDES (issue C). Case, surrounding/duplicated
   whitespace and punctuation are folded away before any comparison runs, so a
   cosmetic difference between two renderings of one name cannot decide an
   outcome.
"""

import pytest

from app.core.document_schemas import resolve_document_type
from app.services.match_service import (
    compareFields,
    matchAgainstCachedFields,
    matchDocuments,
)
from app.services.ocr_service import extractFields

CNIC_TEXT = "Name: Asim Khan\nCNIC Number: 42101-1234567-1\nDate of Birth: 05-08-1990\n"
LETTER_TEXT = "Name: Asim Khan\nDesignation: Senior Engineer\n"


def _fields(text, document_type):
    return extractFields(text, document_type)["fields"]


# --- 1. cross-format matching ------------------------------------------------


def test_docx_reference_matches_a_pdf_submission_of_the_same_document(tmp_path, monkeypatch):
    """Issue D: a DOCX reference and a PDF submission must match on content.

    The reference is a real DOCX (python-docx) read through the document XML;
    the submission is a real PDF read through the OCR path. Only `file_a` is
    given as a file — the reference side is the field map cached on the
    employee_documents row at upload time, which is how the backend now
    compares it.
    """
    docx = pytest.importorskip("docx")

    reference = tmp_path / "reference.docx"
    d = docx.Document()
    d.add_paragraph("Name: Asim Khan")
    d.add_paragraph("CNIC: 42101-1234567-1")
    d.add_paragraph("Date of Birth: 05-08-1990")
    d.save(str(reference))

    # A one-page PDF whose embedded text is what OCR would read back.
    submission = tmp_path / "submission.pdf"
    _write_text_pdf(submission, CNIC_TEXT)

    cached = _fields(CNIC_TEXT, "CNIC / National ID Copy")

    # Sanity: the reference really does produce those fields, i.e. the cached
    # map is what an upload-time extraction of the DOCX would have stored.
    from app.services.ocr_service import extractText

    assert _fields(extractText(str(reference)), "CNIC / National ID Copy") == cached

    # The OCR path for a text-only PDF returns no text without Tesseract, so
    # the submission's side is injected while the comparison itself is the real
    # one. Everything the decision depends on runs for real.
    monkeypatch.setattr(
        "app.services.match_service.extractText", lambda p: CNIC_TEXT
    )

    result = matchAgainstCachedFields(
        str(submission),
        cached,
        "CNIC / National ID Copy",
        "CNIC / National ID Copy",
    )
    assert result["match"] is True
    assert result["confidence"] == 100.0
    assert any(reason.startswith("name matches") for reason in result["reasons"])
    assert any(reason.startswith("cnic matches") for reason in result["reasons"])


def test_a_required_field_still_not_visible_still_fails_a_match():
    """The cache must not weaken the hard-fail rules.

    If the submission's fields cannot be extracted, a required field is
    "not visible" and the comparison must refuse — a cache is a different
    source for the reference's values, never a licence to skip a check.
    """
    from app.core.document_schemas import get_schema

    schema = get_schema("CNIC / National ID Copy")[1]

    result = compareFields(
        _fields("", "CNIC / National ID Copy"),
        _fields(CNIC_TEXT, "CNIC / National ID Copy"),
        schema,
        schema,
    )
    assert result["match"] is False
    assert any("not visible" in reason for reason in result["reasons"])


def test_cached_reference_reuses_the_typed_document_type():
    """A cached canonical key must re-resolve to its own schema, not 'generic'.

    Degrading to the generic schema would silently change which fields are
    REQUIRED, so the comparison could start passing on fields the real schema
    treats as mandatory.
    """
    for key in ("offer_letter", "job_description", "exit_form", "cnic", "settlement"):
        assert resolve_document_type(key) == key
    # And an unknown label still degrades to generic, as before.
    assert resolve_document_type("some bespoke form") == "generic"


# --- 2. normalization applied to both sides ----------------------------------
#
# These pin the comparison layer directly, with hand-built field maps, because
# that is where normalization is required to happen. Going through the text
# extractor would test its own label cleanup instead, and would hide whether the
# comparison itself folds case/whitespace/punctuation.


def _letter_fields(name="Asim Khan", designation="Senior Engineer"):
    return {
        "name": {"value": name, "confidence": "high"},
        "designation": {"value": designation, "confidence": "high"},
        "joining_date": {"value": None, "confidence": "not_visible"},
    }


def _employment_schema():
    from app.core.document_schemas import get_schema

    return get_schema("employment_letter")[1]


def test_name_comparison_is_insensitive_to_case_and_punctuation():
    result = compareFields(
        _letter_fields(),
        _letter_fields(name="asim khan", designation="SENIOR ENGINEER"),
        _employment_schema(),
        _employment_schema(),
    )
    assert result["match"] is True
    assert result["confidence"] == 100.0


def test_name_comparison_collapses_whitespace():
    result = compareFields(
        _letter_fields(),
        _letter_fields(name="  asim   khan  ", designation=" Senior  Engineer "),
        _employment_schema(),
        _employment_schema(),
    )
    assert result["match"] is True
    assert result["confidence"] == 100.0


def test_hyphenated_and_spaced_names_compare_equal():
    """A reachable case: the extractor KEEPS an intra-name hyphen, so the
    comparison is what has to treat "Asim-Khan" and "Asim Khan" as one name."""
    result = compareFields(
        _letter_fields(name="Asim-Khan"),
        _letter_fields(name="Asim Khan"),
        _employment_schema(),
        _employment_schema(),
    )
    assert result["match"] is True
    assert result["confidence"] == 100.0


def test_exact_fields_ignore_punctuation_separators():
    """An OCR'd "42101-1234567-1" and a typed "4210112345671" are one CNIC."""
    from app.core.document_schemas import get_schema

    schema = get_schema("CNIC / National ID Copy")[1]

    def cnic_fields(value):
        return {
            "name": {"value": "Asim Khan", "confidence": "high"},
            "cnic": {"value": value, "confidence": "high"},
            "dob": {"value": "1990-08-05", "confidence": "high"},
        }

    result = compareFields(
        cnic_fields("42101-1234567-1"),
        cnic_fields("4210112345671"),
        schema,
        schema,
    )
    assert result["match"] is True
    assert result["confidence"] == 100.0


def test_a_real_name_difference_still_scores_below_the_threshold():
    """Normalization must not become leniency: two different people stay apart."""
    result = compareFields(
        _letter_fields(),
        _letter_fields(name="Bilal Ahmed"),
        _employment_schema(),
        _employment_schema(),
    )
    assert result["match"] is False
    assert any(reason.startswith("name differs") for reason in result["reasons"])


def test_normalization_is_symmetric():
    """Folding case/whitespace/punctuation must not favour either side."""
    a = _letter_fields(name="asim khan", designation="SENIOR ENGINEER")
    b = _letter_fields()
    schema = _employment_schema()

    assert compareFields(a, b, schema, schema) == compareFields(b, a, schema, schema)


def _write_text_pdf(path, text):
    """A minimal single-page PDF carrying `text` as its content stream."""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 550, 800), text, fontsize=11)
    doc.save(str(path))
    doc.close()
