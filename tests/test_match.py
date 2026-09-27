"""Endpoint tests for POST /match — true and false paths.

OCR text is injected (no Tesseract needed) by patching the `extractText` name
INSIDE match_service so the real extraction + canonical-field comparison runs
end to end. The patched reader picks the synthetic text from the uploaded
file's content marker, so file_a and file_b can differ.

The cached-field cases feed one side of the comparison as a canonical field map
extracted earlier (what the backend stores on an employee reference document at
upload time) instead of a file, which is how a reference is compared without
being re-read.
"""

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}

CNIC_A_TEXT = "Name: Asim Khan\nCNIC Number: 42101-1234567-1\nDate of Birth: 05-08-1990\n"
CNIC_B_TEXT = "Name: Asim Khan\nCNIC Number: 35202-7654321-1\nDate of Birth: 05-08-1990\n"
OFFER_TEXT = "Name: Asim Khan\nDesignation: Software Engineer\n"


@pytest.fixture
def cached_cnic_fields():
    """The canonical map extractFields() would return for CNIC_A_TEXT."""
    from app.services.ocr_service import extractFields

    return extractFields(CNIC_A_TEXT, "CNIC / National ID Copy")["fields"]


@pytest.fixture
def fake_match_ocr(monkeypatch):
    def fake_extract_text(path):
        with open(path, "rb") as fh:
            body = fh.read()
        if b"CNIC-B" in body:
            return CNIC_B_TEXT
        if b"OFFER" in body:
            return OFFER_TEXT
        return CNIC_A_TEXT

    monkeypatch.setattr("app.services.match_service.extractText", fake_extract_text)


def _match(marker_a, marker_b, type_a="CNIC / National ID Copy", type_b="CNIC / National ID Copy"):
    return client.post(
        "/match",
        files={
            "file_a": (f"{marker_a}.txt", io.BytesIO(marker_a.encode()), "text/plain"),
            "file_b": (f"{marker_b}.txt", io.BytesIO(marker_b.encode()), "text/plain"),
        },
        data={"document_type_a": type_a, "document_type_b": type_b},
        headers=HEADERS,
    )


def test_match_true_when_identity_fields_agree(fake_match_ocr):
    resp = _match("CNIC-A-1", "CNIC-A-2")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["match"] is True
    assert data["confidence"] == 100.0


def test_match_false_when_required_cnic_differs(fake_match_ocr):
    resp = _match("CNIC-A-1", "CNIC-B-2")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["match"] is False
    assert data["confidence"] == 0.0
    assert any(reason.startswith("cnic differs") for reason in data["reasons"])


def test_match_true_cross_type_skips_absent_optional_field(fake_match_ocr):
    # Two offer letters, both missing the optional joining_date. The absent
    # optional field is skipped; name + designation agree, so the pair matches.
    resp = _match("OFFER-A", "OFFER-B", "offer letter", "offer letter")
    assert resp.status_code == 200
    assert resp.json()["data"]["match"] is True
    assert resp.json()["data"]["confidence"] == 100.0


def test_match_requires_both_sides(fake_match_ocr):
    # A side may now be given as a pre-extracted field map instead of a file, so
    # the file arguments are optional at the schema level and the handler is the
    # one that rejects a missing side (400). The message names both ways of
    # supplying it, so a caller knows cached fields are a valid alternative.
    resp = client.post(
        "/match",
        files={"file_a": ("a.txt", io.BytesIO(b"x"), "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "file_b" in resp.json()["detail"]

    resp = client.post(
        "/match",
        files={"file_b": ("b.txt", io.BytesIO(b"x"), "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "file_a" in resp.json()["detail"]


def test_match_rejects_malformed_cached_fields(fake_match_ocr):
    # A cache that cannot be decoded must fail loudly. Silently ignoring it
    # would fall through to a comparison the caller did not ask for — the exact
    # quiet-wrong-answer this path must never produce.
    resp = client.post(
        "/match",
        files={"file_a": ("CNIC-A-1", io.BytesIO(b"CNIC-A-1"), "text/plain")},
        data={"file_b_fields": "{not json"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "not valid JSON" in resp.json()["detail"]

    resp = client.post(
        "/match",
        files={"file_a": ("CNIC-A-1", io.BytesIO(b"CNIC-A-1"), "text/plain")},
        data={"file_b_fields": '{"fields": {}}'},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "non-empty" in resp.json()["detail"]


def test_match_against_cached_reference_fields(fake_match_ocr, cached_cnic_fields):
    # The submission is a freshly extracted file; the reference side is a field
    # map cached at upload time. The comparison must run on the canonical fields
    # only, so the reference's file never has to exist on disk.
    resp = client.post(
        "/match",
        files={"file_a": ("CNIC-A-1", io.BytesIO(b"CNIC-A-1"), "text/plain")},
        data={
            "document_type_a": "CNIC / National ID Copy",
            "document_type_b": "CNIC / National ID Copy",
            "file_b_fields": json.dumps({"fields": cached_cnic_fields}),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["match"] is True
    assert data["confidence"] == 100.0


def test_match_against_cached_reference_fields_detects_a_different_person(
    fake_match_ocr, cached_cnic_fields
):
    resp = client.post(
        "/match",
        files={"file_a": ("CNIC-B-2", io.BytesIO(b"CNIC-B-2"), "text/plain")},
        data={
            "document_type_a": "CNIC / National ID Copy",
            "document_type_b": "CNIC / National ID Copy",
            "file_b_fields": json.dumps({"fields": cached_cnic_fields}),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["match"] is False
    assert any(reason.startswith("cnic differs") for reason in data["reasons"])


def test_match_cached_side_may_be_file_a(fake_match_ocr, cached_cnic_fields):
    # Symmetry: either side can be the cache. compareFields is symmetric under
    # swapping, so the verdict is identical.
    resp = client.post(
        "/match",
        files={"file_b": ("CNIC-A-1", io.BytesIO(b"CNIC-A-1"), "text/plain")},
        data={
            "document_type_a": "CNIC / National ID Copy",
            "document_type_b": "CNIC / National ID Copy",
            "file_a_fields": json.dumps({"fields": cached_cnic_fields}),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["match"] is True
