"""Identity binding: a document may only be matched to the person it names.

The /match endpoint used to decide identity from an AVERAGE over whatever
canonical fields happened to be comparable. Three gaps followed from that, all
pinned here:

1. A CNIC that OCR could read was still invisible to the comparison whenever the
   document type's schema did not declare `cnic`. extractFields() only ever emits
   the fields a schema declares, so for 35 of the 39 types a CNIC printed on the
   page was extracted and then thrown away. A CNIC-copy submission compared
   against an employment letter therefore had only `name` in common and returned
   confidence 100.0 -- two visibly different people auto-approving on a name.

2. Even with `cnic` declared, a CNIC present on ONE side only was silently
   skipped by the "optional field absent on one side" rule. Same outcome: 100.0.

3. Identity-poor document types -- a photograph, an NDA, a policy acknowledgment,
   a handover form -- were compared like any other. `name` is optional on all of
   them, so two NDAs sharing a name scored 100.0 and auto-approved, which
   establishes nothing: those documents describe a policy, not a person.

The fix makes the CNIC an always-compared field on every schema, refuses an
identity-poor type BEFORE any extraction runs, and reports two flags so the
caller can act on a boolean instead of re-deriving the policy:
`identity_mismatch` and `auto_match_eligible`.

THE DILUTION HOLE is the reason the flag exists at all and is pinned by
test_a_mismatched_cnic_is_not_averaged_away below: scoring the CNIC 0 is not
sufficient on its own, because a document type carrying many matching
non-identity fields can still average above the auto-approve bar -- to the point
where `match` itself comes back True. The score is reduced AND the flag is
raised, and the caller must honour the flag.
"""

import io

import pytest
from fastapi.testclient import TestClient

from app.core.document_schemas import (
    AUTO_MATCH_INELIGIBLE_TYPES,
    DOCUMENT_TYPE_SCHEMAS,
    get_schema,
    is_auto_match_eligible,
    resolve_document_type,
)
from app.main import app
from app.services.match_service import compareFields, matchDocuments
from app.services.ocr_service import extractFields

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}

PERSON_A = "42101-1234567-1"
PERSON_B = "35202-7654321-9"

# Full catalog labels. NOTE: the bare string "NDA" does NOT resolve to the
# legal_agreement schema -- it falls through to the generic fallback -- so these
# tests use the catalog labels the product actually sends.
NDA_LABEL = "NDA — Non-Disclosure Agreement"
PHOTO_LABEL = "Recent Photograph"
OFFER_LABEL = "Offer Letter"

# Letters that carry a CNIC for each person -- the case that used to be invisible
# to the comparison because offer_letter did not declare a cnic field.
LETTER_A = f"Employee Name: Asim Khan\nCNIC: {PERSON_A}\nDesignation: Engineer\nDate of Joining: 2024-01-01\n"
LETTER_B = f"Employee Name: Asim Khan\nCNIC: {PERSON_B}\nDesignation: Engineer\nDate of Joining: 2024-01-01\n"
LETTER_NO_CNIC = "Employee Name: Asim Khan\nDesignation: Engineer\nDate of Joining: 2024-01-01\n"


def fields_for(text, document_type):
    """The canonical map extractFields() would really produce."""
    return extractFields(text, document_type)["fields"]


def schema_for(document_type):
    return get_schema(document_type)[1]


# --- 1. every schema declares cnic --------------------------------------------


def test_a_cnic_on_a_letter_reaches_the_comparison():
    result = compareFields(
        fields_for(LETTER_A, OFFER_LABEL),
        fields_for(LETTER_B, OFFER_LABEL),
        schema_for(OFFER_LABEL),
        schema_for(OFFER_LABEL),
    )
    # It is compared at all, and it disagrees.
    assert any("cnic differs" in reason for reason in result["reasons"])
    assert result["identity_mismatch"] is True
    assert result["auto_match_eligible"] is False


def test_two_letters_naming_the_same_person_still_match():
    result = compareFields(
        fields_for(LETTER_A, OFFER_LABEL),
        fields_for(LETTER_A, OFFER_LABEL),
        schema_for(OFFER_LABEL),
        schema_for(OFFER_LABEL),
    )
    assert result["identity_mismatch"] is False
    assert result["auto_match_eligible"] is True
    assert result["confidence"] == 100.0
    assert result["match"] is True


# --- 2. a one-sided CNIC is not silently skipped ------------------------------


def test_a_cnic_on_one_side_only_is_never_dropped():
    # The submission shows a CNIC; the reference letter does not. There is no
    # evidence the two describe the same person, so the score must not be 100.
    result = compareFields(
        fields_for(LETTER_A, OFFER_LABEL),
        fields_for(LETTER_NO_CNIC, OFFER_LABEL),
        schema_for(OFFER_LABEL),
        schema_for(OFFER_LABEL),
    )
    assert result["identity_mismatch"] is True
    assert result["auto_match_eligible"] is False
    assert result["confidence"] < 100.0
    assert any("identity could not be confirmed" in reason for reason in result["reasons"])


def test_a_cnic_absent_from_both_documents_is_simply_no_evidence():
    # Neither side shows one: nothing to compare, nothing to fail. This is the
    # ordinary case for a letter, and it must stay eligible.
    result = compareFields(
        fields_for(LETTER_NO_CNIC, OFFER_LABEL),
        fields_for(LETTER_NO_CNIC, OFFER_LABEL),
        schema_for(OFFER_LABEL),
        schema_for(OFFER_LABEL),
    )
    assert result["identity_mismatch"] is False
    assert result["auto_match_eligible"] is True
    assert result["confidence"] == 100.0


def test_a_cnic_that_could_not_be_read_is_not_the_same_as_absent():
    # An unreadable CNIC is a statement about the SCAN, not about the person, so
    # it must not be treated as a confirmed difference -- but the other side
    # DOES show one, so identity is still unconfirmed and must be flagged.
    result = compareFields(
        fields_for(LETTER_A, OFFER_LABEL),
        {
            **fields_for(LETTER_NO_CNIC, OFFER_LABEL),
            "cnic": {"value": None, "confidence": "not_visible"},
        },
        schema_for(OFFER_LABEL),
        schema_for(OFFER_LABEL),
    )
    assert result["identity_mismatch"] is True


def test_a_cnic_missing_from_the_reference_map_entirely_is_still_compared():
    """A cached reference extracted BEFORE every schema declared `cnic`.

    The reference's cached field map has no `cnic` KEY at all, not a key with a
    null value. Restricting the comparison to the intersection of the two maps
    dropped it silently, leaving `name` as the only comparable field -- and a
    CNIC-copy submission then matched an employment reference at 100.0. This is
    the exact case that survived the first attempt at this fix; a missing side
    must read as "not visible", never as "not applicable".
    """
    reference_map = fields_for(LETTER_NO_CNIC, OFFER_LABEL)
    assert "cnic" in reference_map  # a fresh extraction does declare it
    del reference_map["cnic"]  # ...but a pre-existing cached one does not

    result = compareFields(
        fields_for(LETTER_A, "CNIC / National ID Copy"),
        reference_map,
        schema_for("CNIC / National ID Copy"),
        schema_for(OFFER_LABEL),
    )

    assert result["confidence"] < 100.0
    assert result["identity_mismatch"] is True
    assert result["auto_match_eligible"] is False
    assert result["match"] is False


def test_a_cnic_missing_from_the_submission_map_entirely_is_still_compared():
    """The mirror image: the reference shows a CNIC, the submission map has none."""
    submission_map = fields_for(LETTER_NO_CNIC, "CNIC / National ID Copy")
    del submission_map["cnic"]

    result = compareFields(
        submission_map,
        fields_for(LETTER_A, "CNIC / National ID Copy"),
        schema_for("CNIC / National ID Copy"),
        schema_for("CNIC / National ID Copy"),
    )

    assert result["identity_mismatch"] is True
    assert result["auto_match_eligible"] is False


# --- 3. the dilution hole -----------------------------------------------------


def test_a_mismatched_cnic_is_not_averaged_away():
    """One zeroed field must not be buried by a high average.

    A type with many comparable non-identity fields can average above the
    auto-approve bar even with the CNIC scoring 0 -- here `match` itself comes
    back True. The score alone would then still read as a confident match, which
    is why the flags exist and why the caller must honour
    `auto_match_eligible` rather than the score.
    """
    schema = {
        "name": True,
        "cnic": False,
        "designation": False,
        "degree": False,
        "session": False,
        "joining_date": False,
        "bank_account": False,
        "increment": False,
        "tax_year": False,
        "duration": False,
        "recommendation_date": False,
    }

    def high(value):
        return {"value": value, "confidence": "high"}

    agreeing = {
        "name": high("Asim Khan"),
        "designation": high("Engineer"),
        "degree": high("BSCS"),
        "session": high("2018"),
        "joining_date": high("2024-01-01"),
        "bank_account": high("1234567890"),
        "increment": high("50000"),
        "tax_year": high("2024"),
        "duration": high("5 years"),
        "recommendation_date": high("2024-02-02"),
    }

    same_person = {**agreeing, "cnic": high("4210112345671")}
    other_person = {**agreeing, "cnic": high("3520276543219")}

    result = compareFields(same_person, other_person, schema, schema)

    # Guard: this test only means something if the arithmetic alone would let the
    # mismatch through. Ten perfect fields and one zero average to 90.9, above
    # MATCH_CONFIDENCE_THRESHOLD.
    assert result["confidence"] > 90
    # ...and `match` is forced back to False, because a `match: true` next to
    # `identity_mismatch: true` would be read as approval by any consumer that
    # only looks at the score.
    assert result["match"] is False
    # The flags are belt-and-braces on top of that, for callers that want to
    # explain the refusal rather than just observe it.
    assert result["identity_mismatch"] is True
    assert result["auto_match_eligible"] is False


def test_an_identity_mismatch_can_never_be_reported_as_a_match():
    """`match` and `identity_mismatch` must be mutually exclusive, always.

    The realistic case is a letter: name, designation and joining_date all agree
    while the printed CNICs differ, which averages to exactly
    MATCH_CONFIDENCE_THRESHOLD. Before `match` was coerced this reported
    `match: true` for two documents belonging to different people.
    """
    result = compareFields(
        fields_for(LETTER_A, OFFER_LABEL),
        fields_for(LETTER_B, OFFER_LABEL),
        schema_for(OFFER_LABEL),
        schema_for(OFFER_LABEL),
    )
    # Averaging three perfect fields with one zeroed CNIC lands on the threshold,
    # so the score on its own would have said "match".
    assert result["confidence"] >= 75.0
    assert result["identity_mismatch"] is True
    assert result["match"] is False
    assert result["auto_match_eligible"] is False


# --- 4. identity-poor document types are refused up front ---------------------


@pytest.mark.parametrize("key", sorted(AUTO_MATCH_INELIGIBLE_TYPES))
def test_identity_poor_types_are_never_auto_match_eligible(key):
    eligible, reason = is_auto_match_eligible(key)
    assert eligible is False
    assert reason  # something a reviewer can be shown


@pytest.mark.parametrize(
    "label",
    [
        PHOTO_LABEL,
        NDA_LABEL,
        "Code of Conduct Agreement",
        "Data Privacy / Confidentiality Agreement",
        "Company Policies Acknowledgment",
        "IT / Computer Usage Policy Acknowledgment",
        "Asset Handover Form",
        "Laptop / Computer Handover Form",
        "SIM / Mobile / Other Equipment Handover",
        "Company Asset Return Form",
        "Employee File Closing Checklist",
        "Joining / Onboarding Checklist",
        "Employee ID Card Record",
        "Job Description",
    ],
)
def test_identity_poor_catalog_labels_resolve_to_an_ineligible_type(label):
    # Every catalog label the product sends for a non-identifying document must
    # actually be gated, including the aliases that share a canonical type.
    eligible, _ = is_auto_match_eligible(label)
    assert eligible is False, f"{label!r} resolved to {resolve_document_type(label)!r}"


@pytest.mark.parametrize(
    "key",
    [
        "cnic",
        "offer_letter",
        "resume",
        "experience_letter",
        "settlement",
        "employment_contract",
        "education_certificate",
        "bank_details",
    ],
)
def test_identity_bearing_types_remain_eligible(key):
    eligible, _ = is_auto_match_eligible(key)
    assert eligible is True


def test_an_ineligible_type_is_refused_without_running_extraction(monkeypatch):
    """The gate runs BEFORE extraction, so declining costs no OCR at all."""
    import app.services.match_service as match_service

    def explode(_path):  # pragma: no cover - must never be reached
        raise AssertionError("extractText must not run for an ineligible type")

    monkeypatch.setattr(match_service, "extractText", explode)

    result = matchDocuments("nda.pdf", "photo.pdf", NDA_LABEL, PHOTO_LABEL)

    assert result["match"] is False
    assert result["confidence"] == 0.0
    assert result["auto_match_eligible"] is False
    assert result["reasons"]


def test_two_ndas_sharing_a_name_no_longer_auto_approve(monkeypatch):
    """The concrete abuse this closes: identical paperwork, different people."""
    import app.services.match_service as match_service

    nda = "Name: Asim Khan\nI agree to keep company information confidential.\n"
    monkeypatch.setattr(match_service, "extractText", lambda _p: nda)

    result = matchDocuments("a.pdf", "b.pdf", NDA_LABEL, NDA_LABEL)

    assert result["match"] is False
    assert result["auto_match_eligible"] is False
    # Not an identity mismatch -- the type itself cannot establish identity.
    assert result["identity_mismatch"] is False


def test_an_ineligible_submission_is_refused_against_an_eligible_reference(monkeypatch):
    """Both sides are gated: a photo cannot ride on an employment reference."""
    import app.services.match_service as match_service

    def explode(_path):  # pragma: no cover - must never be reached
        raise AssertionError("extractText must not run when either side is ineligible")

    monkeypatch.setattr(match_service, "extractText", explode)

    result = matchDocuments("photo.jpg", "letter.pdf", PHOTO_LABEL, OFFER_LABEL)
    assert result["auto_match_eligible"] is False


# --- 5. the endpoint surfaces the flags ---------------------------------------


def _body(path):
    with open(path, "rb") as handle:
        return handle.read()


def _match(marker_a, marker_b, type_a, type_b, monkeypatch):
    import app.services.match_service as match_service

    texts = {b"PERSON-A": LETTER_A, b"PERSON-B": LETTER_B, b"NO-CNIC": LETTER_NO_CNIC}
    monkeypatch.setattr(match_service, "extractText", lambda p: texts[_body(p)])
    return client.post(
        "/match",
        files={
            "file_a": (f"{marker_a}.txt", io.BytesIO(marker_a.encode()), "text/plain"),
            "file_b": (f"{marker_b}.txt", io.BytesIO(marker_b.encode()), "text/plain"),
        },
        data={"document_type_a": type_a, "document_type_b": type_b},
        headers=HEADERS,
    )


def test_endpoint_reports_identity_mismatch(monkeypatch):
    resp = _match("PERSON-A", "PERSON-B", OFFER_LABEL, OFFER_LABEL, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["identity_mismatch"] is True
    assert data["auto_match_eligible"] is False
    assert data["match"] is False


def test_endpoint_reports_a_clean_comparison(monkeypatch):
    resp = _match("PERSON-A", "PERSON-A", OFFER_LABEL, OFFER_LABEL, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["identity_mismatch"] is False
    assert data["auto_match_eligible"] is True
    assert data["confidence"] == 100.0


def test_endpoint_reports_an_ineligible_document_type(monkeypatch):
    resp = _match("PERSON-A", "PERSON-A", NDA_LABEL, NDA_LABEL, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["auto_match_eligible"] is False
    assert data["confidence"] == 0.0


def test_flags_default_to_permissive_for_an_older_service_build():
    """A build that does not send the flags must not be read as a failure.

    The response model defaults them, so a Node backend running against a
    pre-flag Python service keeps its previous behaviour instead of refusing
    every request.
    """
    from app.schemas.match import MatchData

    legacy = MatchData(match=True, confidence=100.0, reasons=[])
    assert legacy.identity_mismatch is False
    assert legacy.auto_match_eligible is True


def test_every_catalog_type_either_declares_cnic_or_is_gated():
    """The two halves of the fix must together cover every document type."""
    for key, schema in DOCUMENT_TYPE_SCHEMAS.items():
        has_cnic = "cnic" in schema
        gated = key in AUTO_MATCH_INELIGIBLE_TYPES
        assert has_cnic or gated, f"{key} declares no cnic and is not gated"
