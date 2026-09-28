"""Unit tests for the canonical document-type schemas, field extraction and
canonical-field matching. No Tesseract / no filesystem I/O (pure string logic).

The catalog-label list below mirrors EMPLOYEE_DOCUMENT_TYPES in
dvarif-verified/src/lib/documentTypes.ts — keep it in sync so the aliases never
silently drift away from the Node catalog.
"""

import pytest  # noqa: F401

from app.core.document_schemas import (
    DOCUMENT_TYPE_SCHEMAS,
    get_schema,
    resolve_document_type,
)
from app.services.match_service import NAME_MATCH_THRESHOLD, compareFields
from app.services.ocr_service import extractCnicAndName, extractFields


def h(value):
    return {"value": value, "confidence": "high"}


def low(value):
    return {"value": value, "confidence": "low"}


def nv():
    return {"value": None, "confidence": "not_visible"}


# --- schema resolution ------------------------------------------------------


def test_resolve_canonical_keys_and_generic_fallback():
    assert resolve_document_type("cnic") == "cnic"
    assert resolve_document_type("passport") == "passport"
    assert resolve_document_type("offer_letter") == "offer_letter"
    assert resolve_document_type(None) == "generic"
    assert resolve_document_type("") == "generic"
    assert resolve_document_type("totally-unknown") == "generic"


def test_resolve_frontend_catalog_labels():
    # Kept in sync with EMPLOYEE_DOCUMENT_TYPES in dvarif-verified/src/lib/documentTypes.ts
    assert resolve_document_type("CNIC / National ID Copy") == "cnic"
    assert resolve_document_type("Passport Copy — if applicable") == "passport"
    assert resolve_document_type("Offer Letter") == "offer_letter"
    assert resolve_document_type("Employment / Appointment Letter") == "appointment_letter"
    assert resolve_document_type("Experience Certificates") == "experience_letter"
    assert resolve_document_type("CV / Resume") == "resume"
    assert resolve_document_type("Educational Transcripts / Mark Sheets") == "transcript"
    assert resolve_document_type("Relieving Letter") == "relieving_letter"


def test_every_catalog_label_resolves_to_a_tracked_schema():
    # Mirrors EMPLOYEE_DOCUMENT_TYPES — keep in sync with documentTypes.ts.
    catalog = [
        "Employee Application Form",
        "CV / Resume",
        "Recent Photograph",
        "CNIC / National ID Copy",
        "Passport Copy — if applicable",
        "Educational Certificates",
        "Educational Transcripts / Mark Sheets",
        "Experience Certificates",
        "Previous Employment / Relieving Letter",
        "Reference / Recommendation Letters",
        "Employee Information Form",
        "Employment / Appointment Letter",
        "Job Description",
        "Offer Letter",
        "Employment Contract / Agreement",
        "NDA — Non-Disclosure Agreement",
        "Company Policies Acknowledgment",
        "Code of Conduct Agreement",
        "IT / Computer Usage Policy Acknowledgment",
        "Data Privacy / Confidentiality Agreement",
        "Bank Account / Salary Details",
        "Tax Information / Tax Documents",
        "Emergency Contact Form",
        "Medical / Fitness Certificate — if required",
        "Background Verification Report — if applicable",
        "Police / Character Certificate — if required",
        "Joining / Onboarding Checklist",
        "Employee ID Card Record",
        "Asset Handover Form",
        "Laptop / Computer Handover Form",
        "SIM / Mobile / Other Equipment Handover",
        "Leave Records",
        "Attendance Records",
        "Performance Evaluation Records",
        "Training / Certification Records",
        "Warning / Disciplinary Records — if applicable",
        "Promotion / Salary Revision Letters",
        "Transfer / Department Change Records",
        "Increment Letter",
        "Resignation Letter",
        "Exit Interview Form",
        "Clearance Form",
        "Final Settlement Record",
        "Experience / Service Certificate",
        "Relieving Letter",
        "Company Asset Return Form",
        "Employee File Closing Checklist",
    ]
    resolved = {label: resolve_document_type(label) for label in catalog}
    assert all(value in DOCUMENT_TYPE_SCHEMAS for value in resolved.values())
    # Every canonical key is reachable from at least one alias position.
    assert resolved["CNIC / National ID Copy"] == "cnic"
    assert resolved["Employee File Closing Checklist"] == "closing_checklist"
    assert len(resolved) == len(catalog)


def test_get_schema_returns_canonical_key_and_field_map():
    key, schema = get_schema("CNIC / National ID Copy")
    assert key == "cnic"
    assert schema["name"] is True
    assert schema["cnic"] is True
    assert schema["dob"] is True

    key, schema = get_schema(None)
    assert key == "generic"
    assert schema == {"name": True, "cnic": False}


# --- canonical field extraction ---------------------------------------------


def test_extract_cnic_fields_canonical():
    text = (
        "NATIONAL IDENTITY CARD\n"
        "Name: Asim Khan\n"
        "CNIC Number: 42101-1234567-1\n"
        "Date of Birth: 05-08-1990\n"
        "Signature:"
    )
    result = extractFields(text, "cnic")
    assert result["document_type"] == "cnic"
    fields = result["fields"]
    assert fields["name"] == h("Asim Khan")
    assert fields["cnic"] == h("4210112345671")
    assert fields["dob"]["value"] == "1990-08-05"
    assert fields["dob"]["confidence"] == "high"


def test_extract_marks_missing_required_fields_not_visible():
    result = extractFields("just some text without labels", "cnic")
    assert result["document_type"] == "cnic"
    fields = result["fields"]
    # All three schema fields present, none extracted -> not_visible.
    assert set(fields.keys()) == {"name", "cnic", "dob"}
    assert fields["name"] == nv()
    assert fields["cnic"] == nv()
    assert fields["dob"] == nv()


def test_extract_offer_letter_canonical_names():
    text = (
        "EMPLOYMENT OFFER\n"
        "Employee Name: Asim Khan\n"
        "Designation: Software Engineer\n"
        "Date of Joining: 2023-01-15"
    )
    result = extractFields(text, "offer_letter")
    assert result["document_type"] == "offer_letter"
    fields = result["fields"]
    # `cnic` is declared on every schema: a CNIC printed on a letter must reach
    # the comparison instead of being dropped at extraction. This text has none,
    # so it is reported as not_visible.
    assert set(fields.keys()) == {"name", "cnic", "designation", "joining_date"}
    assert fields["cnic"] == nv()
    assert fields["name"] == h("Asim Khan")
    assert fields["designation"] == h("Software Engineer")
    assert fields["joining_date"] == h("2023-01-15")


def test_a_cnic_printed_on_a_letter_is_actually_extracted():
    # The reason `cnic` is declared on every schema: before, the text was read
    # and the number was simply thrown away, so two letters carrying visibly
    # different CNICs compared on their name alone.
    text = "Employee Name: Asim Khan\nCNIC: 42101-1234567-1\nDesignation: Engineer\n"
    fields = extractFields(text, "offer_letter")["fields"]
    assert fields["cnic"] == h("4210112345671")


def test_every_schema_declares_cnic():
    # A schema without `cnic` is a schema in which a CNIC is invisible to the
    # matcher. assert set(...) rather than a count so a newly added type cannot
    # reintroduce the gap by omission.
    for key, schema in DOCUMENT_TYPE_SCHEMAS.items():
        assert "cnic" in schema, f"{key} does not declare cnic"
    assert "cnic" in get_schema(None)[1]  # the generic fallback too


def test_extract_optional_field_with_weak_label_is_low_confidence():
    result = extractFields("Name: Asim Khan\nDesignation:\nEngineer", "offer_letter")
    # Designation label line is empty; the value is probed from the following
    # line -> weak adjacency -> low confidence.
    assert result["fields"]["designation"] == low("Engineer")
    assert result["fields"]["name"] == h("Asim Khan")


def test_extractCnicAndName_legacy_wrapper():
    assert extractCnicAndName("Name: Asim Khan\nCNIC: 42101-1234567-1") == ("Asim Khan", "4210112345671")
    assert extractCnicAndName("no useful text") == (None, None)


# --- canonical-field matching ------------------------------------------------


def _schema(key):
    return get_schema(key)[1]


def test_match_happy_same_identity_cnic():
    schema = _schema("cnic")
    a = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": h("1990-08-05")}
    b = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": h("1990-08-05")}
    result = compareFields(a, b, schema, schema)
    assert result["match"] is True
    assert result["confidence"] == 100.0


def test_match_fails_hard_when_required_field_not_visible():
    schema = _schema("cnic")
    a = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": nv()}
    b = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": nv()}
    result = compareFields(a, b, schema, schema)
    assert result["match"] is False
    assert result["confidence"] == 0.0
    assert any("dob" in reason and "required" in reason for reason in result["reasons"])


def test_match_fails_hard_on_required_cnic_difference():
    schema = _schema("cnic")
    a = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": h("1990-08-05")}
    b = {"name": h("Asim Khan"), "cnic": h("4210112345672"), "dob": h("1990-08-05")}
    result = compareFields(a, b, schema, schema)
    assert result["match"] is False


def test_match_fails_hard_on_low_confidence_required_mismatch():
    schema = _schema("cnic")
    a = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": low("05-08-1990")}
    b = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": low("06-08-1990")}
    result = compareFields(a, b, schema, schema)
    assert result["match"] is False
    assert any("low-confidence" in reason for reason in result["reasons"])


def test_match_skips_optional_field_absent_on_one_side():
    schema = _schema("offer_letter")
    a = {"name": h("Asim Khan"), "designation": h("Engineer"), "joining_date": nv()}
    b = {"name": h("Asim Khan"), "designation": h("Engineer"), "joining_date": nv()}
    # joining_date is optional: not visible on both -> skipped, not a failure.
    result = compareFields(a, b, schema, schema)
    assert result["match"] is True
    assert result["confidence"] == 100.0


def test_match_rejects_different_names():
    schema = _schema("resume")
    a = {"name": h("Asim Khan"), "designation": h("Engineer"), "cnic": nv()}
    b = {"name": h("Usman Ali"), "designation": h("Engineer"), "cnic": nv()}
    result = compareFields(a, b, schema, schema)
    assert result["match"] is False
    assert result["confidence"] < NAME_MATCH_THRESHOLD


def test_match_crosses_document_types_on_shared_fields():
    """A CNIC-copy reference and an offer-letter submission still share a field.

    The cross-type aspect still works — `name` is compared across the two
    schemas. What this test USED to assert, however, was the bug itself: that
    the pair matched at 100.0, because the only shared field was `name` and the
    CNIC the reference carried was dropped as an unshared key. A document showing
    one person's CNIC must not be reported as a clean match against a document
    showing no CNIC at all, so the CNIC is now compared on its own footing and
    the pair is refused.
    """
    schema_a = _schema("offer_letter")   # name, cnic, designation, joining_date
    schema_b = _schema("cnic")           # name, cnic, dob
    a = {"name": h("Asim Khan"), "designation": h("Engineer"), "joining_date": nv()}
    b = {"name": h("Asim Khan"), "cnic": h("4210112345671"), "dob": h("1990-08-05")}

    result = compareFields(a, b, schema_a, schema_b)

    # The shared, comparable field is still found and still scored.
    assert any(reason.startswith("name matches") for reason in result["reasons"])
    # ...but the unshared CNIC is no longer silently discarded.
    assert result["identity_mismatch"] is True
    assert result["auto_match_eligible"] is False
    assert result["match"] is False
    assert result["confidence"] < 100.0


def test_match_no_comparable_fields():
    schema = _schema("photo")  # {"name": False}
    result = compareFields({"name": nv()}, {"name": nv()}, schema, schema)
    assert result["match"] is False
    assert result["confidence"] == 0.0
    assert "No comparable" in result["reasons"][0]