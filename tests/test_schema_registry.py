"""The admin-defined document-type catalogue, pushed from the Node backend.

The document-type list used to be a hard-coded array in the frontend plus a
hard-coded alias table here, so adding a type meant editing code in two
repositories and redeploying both. The catalogue is now a table in the Node
database, and the backend pushes label -> schema here so a type added from the
admin UI actually resolves to a real extraction schema instead of silently
degrading to 'generic'.

These tests pin the properties that make that trustworthy:

  * the database's mapping WINS over the built-in tables, so a corrected schema
    for an already-known label takes effect immediately;
  * an unknown schema_key is REJECTED, not stored, and resolution falls back to
    'generic' -- a bad row must degrade extraction, never break it;
  * the push REPLACES rather than merges, so a deleted type stops resolving;
  * with nothing pushed, every previously-supported label still resolves exactly
    as before, so a service that never receives a sync is no worse off.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.document_schemas import (
    describe_schemas,
    resolve_document_type,
    set_dynamic_label_map,
)
from app.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}


@pytest.fixture(autouse=True)
def clear_catalogue():
    """Never let one test's push leak into the next."""
    set_dynamic_label_map([])
    yield
    set_dynamic_label_map([])


# --- resolution --------------------------------------------------------------


def test_a_pushed_label_resolves_to_its_schema():
    set_dynamic_label_map([{"label": "Driving Licence Scan", "schema_key": "cnic"}])
    assert resolve_document_type("Driving Licence Scan") == "cnic"


def test_the_pushed_mapping_wins_over_the_built_in_aliases():
    # 'Offer Letter' already maps to offer_letter through the built-in aliases.
    # A catalogue that says otherwise must be obeyed -- that is the whole point of
    # the database being the source of truth.
    set_dynamic_label_map([{"label": "Offer Letter", "schema_key": "employment_contract"}])
    assert resolve_document_type("Offer Letter") == "employment_contract"


def test_resolution_is_case_and_punctuation_insensitive():
    set_dynamic_label_map([{"label": "Bank Statement", "schema_key": "bank_details"}])
    for variant in ["Bank Statement", "bank statement", "  BANK   statement ", "Bank/Statement"]:
        assert resolve_document_type(variant) == "bank_details"


def test_an_unknown_schema_key_is_rejected_and_degrades_to_generic():
    report = set_dynamic_label_map([{"label": "Weird Form", "schema_key": "does_not_exist"}])
    assert report["stored"] == 0
    assert report["rejected"]
    assert report["rejected"][0]["reason"] == "unknown schema_key for this service build"
    # Degrades to a real, working schema rather than raising inside a request.
    assert resolve_document_type("Weird Form") == "generic"


def test_a_label_with_no_schema_key_falls_back_to_generic():
    # The admin can add a type and leave the schema on 'generic'. That is a
    # legitimate choice, not an error.
    set_dynamic_label_map([{"label": "Custom Internal Form", "schema_key": "generic"}])
    assert resolve_document_type("Custom Internal Form") == "generic"


def test_the_push_replaces_so_a_deleted_type_stops_resolving():
    set_dynamic_label_map([{"label": "Temporary Type", "schema_key": "resume"}])
    assert resolve_document_type("Temporary Type") == "resume"
    # Deleted in the database -> the next push no longer contains it.
    set_dynamic_label_map([])
    assert resolve_document_type("Temporary Type") == "generic"


def test_malformed_entries_are_skipped_without_losing_the_good_ones():
    report = set_dynamic_label_map(
        [
            {"label": "Good Form", "schema_key": "resume"},
            {"schema_key": "resume"},  # no label
            {"label": "   ", "schema_key": "resume"},  # blank label
            {"label": "Bad Schema", "schema_key": "nope"},
            "not-a-dict",  # wrong type
        ]
    )
    assert report["stored"] == 1
    assert resolve_document_type("Good Form") == "resume"
    assert resolve_document_type("Bad Schema") == "generic"


def test_with_nothing_pushed_every_built_in_label_still_resolves():
    # The safety property: a service that never receives a sync must behave
    # exactly as it did before this feature existed.
    assert resolve_document_type("CNIC / National ID Copy") == "cnic"
    assert resolve_document_type("Offer Letter") == "offer_letter"
    assert resolve_document_type("NDA — Non-Disclosure Agreement") == "legal_agreement"
    assert resolve_document_type("Recent Photograph") == "photo"
    assert resolve_document_type("Something Nobody Knows") == "generic"


def test_a_pushed_canonical_key_also_still_resolves():
    # Cached field maps carry the canonical key, not the display label, so a
    # re-resolve must keep working.
    assert resolve_document_type("offer_letter") == "offer_letter"


# --- describe_schemas ---------------------------------------------------------


def test_describe_schemas_lists_fields_required_and_eligibility():
    described = describe_schemas()

    assert "cnic" in described["schemas"]
    cnic = described["schemas"]["cnic"]
    assert set(cnic["fields"]) == {"name", "cnic", "dob"}
    assert "cnic" in cnic["required"]
    assert cnic["auto_match_eligible"] is True

    # An identity-poor type is reported as ineligible so the admin UI can warn
    # before someone picks it for a type that should auto-match.
    assert described["schemas"]["photo"]["auto_match_eligible"] is False
    assert "legal_agreement" in described["auto_match_ineligible"]
    assert "generic" in described


def test_describe_schemas_counts_the_pushed_catalogue():
    set_dynamic_label_map(
        [
            {"label": "A", "schema_key": "resume"},
            {"label": "B", "schema_key": "cnic"},
        ]
    )
    assert describe_schemas()["catalogue_count"] == 2


# --- endpoints ----------------------------------------------------------------


def test_get_schemas_is_api_key_protected():
    assert client.get("/schemas").status_code == 401
    assert client.get("/schemas", headers=HEADERS).status_code == 200


def test_put_schemas_replaces_the_catalogue():
    resp = client.put(
        "/schemas",
        json={"types": [{"label": "Degree Transcript Scan", "schema_key": "transcript"}]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["stored"] == 1
    assert resp.json()["data"]["rejected_count"] == 0
    assert resolve_document_type("Degree Transcript Scan") == "transcript"


def test_put_schemas_reports_rejected_entries():
    resp = client.put(
        "/schemas",
        json={"types": [{"label": "Bad", "schema_key": "imaginary_schema"}]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["stored"] == 0
    assert data["rejected_count"] == 1


def test_put_schemas_rejects_an_oversized_catalogue():
    resp = client.put(
        "/schemas",
        json={"types": [{"label": f"T{i}", "schema_key": "generic"} for i in range(501)]},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_resolve_endpoint_reports_what_each_label_became():
    # Push first, then ask — /schemas/resolve must not install what it is given,
    # or it could never answer "does the service still know this label?".
    client.put(
        "/schemas",
        json={"types": [{"label": "Payslip Scan", "schema_key": "bank_details"}]},
        headers=HEADERS,
    )
    resp = client.post(
        "/schemas/resolve",
        json={"types": [{"label": "Payslip Scan", "schema_key": "cnic"}]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    # The pushed mapping (bank_details) wins; the label in the request body is
    # only the question being asked, not a new instruction.
    assert resp.json()["data"]["resolved"] == [
        {"label": "Payslip Scan", "schema_key": "bank_details"}
    ]


def test_resolve_endpoint_does_not_change_the_catalogue():
    client.put(
        "/schemas",
        json={"types": [{"label": "Payslip Scan", "schema_key": "bank_details"}]},
        headers=HEADERS,
    )
    before = describe_schemas()["catalogue_count"]

    # Ask about a type that was never pushed. If resolve pushed its own payload,
    # this would answer "yes" and the question would be meaningless.
    client.post(
        "/schemas/resolve",
        json={"types": [{"label": "Never Registered", "schema_key": "cnic"}]},
        headers=HEADERS,
    )
    assert describe_schemas()["catalogue_count"] == before
    assert resolve_document_type("Never Registered") == "generic"


def test_a_pushed_type_actually_changes_extraction():
    """The end-to-end point: a type added in the admin UI is not just listed,
    it is EXTRACTED with the schema the admin chose."""
    from app.services.ocr_service import extractFields

    client.put(
        "/schemas",
        json={"types": [{"label": "Service Letter", "schema_key": "cnic"}]},
        headers=HEADERS,
    )

    text = "Name: Asim Khan\nCNIC: 42101-1234567-1\nDate of Birth: 05-08-1990\n"

    # As its own schema it would only try name + cnic.
    as_cnic = extractFields(text, "Service Letter")["fields"]
    assert set(as_cnic) == {"name", "cnic", "dob"}
    assert as_cnic["dob"]["value"] == "1990-08-05"

    # Pointed at a schema that declares no dob, the same text yields no dob --
    # proving the resolution above is what drove the extraction.
    client.put(
        "/schemas",
        json={"types": [{"label": "Service Letter", "schema_key": "experience_letter"}]},
        headers=HEADERS,
    )
    as_letter = extractFields(text, "Service Letter")["fields"]
    assert "dob" not in as_letter
