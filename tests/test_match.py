"""Endpoint tests for POST /match — true and false paths.

OCR text is injected (no Tesseract needed) by patching the `extractText` name
INSIDE match_service so the real extraction + canonical-field comparison runs
end to end. The patched reader picks the synthetic text from the uploaded
file's content marker, so file_a and file_b can differ.
"""

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}

CNIC_A_TEXT = "Name: Asim Khan\nCNIC Number: 42101-1234567-1\nDate of Birth: 05-08-1990\n"
CNIC_B_TEXT = "Name: Asim Khan\nCNIC Number: 35202-7654321-1\nDate of Birth: 05-08-1990\n"
OFFER_TEXT = "Name: Asim Khan\nDesignation: Software Engineer\n"


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


def test_match_requires_both_files(fake_match_ocr):
    # A missing required multipart upload is rejected by FastAPI itself (422)
    # before the route handler runs.
    resp = client.post(
        "/match",
        files={"file_a": ("a.txt", io.BytesIO(b"x"), "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 422