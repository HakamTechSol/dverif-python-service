"""Endpoint tests for POST /ocr/extract across two document-type schemas.

OCR text is injected (no Tesseract needed) by patching the `extractText` name
the route calls, so the real schema-driven `extractFields` pipeline runs on
deterministic text.
"""

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}

CNIC_TEXT = (
    "Islamic Republic of Pakistan\n"
    "Name: Asim Khan\n"
    "Father Name: Muhammad Khan\n"
    "CNIC Number: 42101-1234567-1\n"
    "Date of Birth: 05-08-1990\n"
)
OFFER_TEXT = (
    "Name: Asim Khan\n"
    "Designation: Software Engineer\n"
    "Date of Joining: 01-06-2023\n"
)


@pytest.fixture
def fake_ocr(monkeypatch):
    holder = {"text": CNIC_TEXT}
    monkeypatch.setattr("app.api.routes.ocr.extractText", lambda _path: holder["text"])
    return holder


def _extract(document_type):
    return client.post(
        "/ocr/extract",
        files={"file": ("doc.txt", io.BytesIO(b"ignored"), "text/plain")},
        data={"document_type": document_type},
        headers=HEADERS,
    )


def test_cnic_schema_via_frontend_catalog_label(fake_ocr):
    resp = _extract("CNIC / National ID Copy")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-Id")
    data = resp.json()["data"]
    assert data["document_type"] == "cnic"
    assert set(data["fields"]) == {"name", "cnic", "dob"}
    assert data["fields"]["name"] == {"value": "Asim Khan", "confidence": "high"}
    # CNIC value is canonical (digit-only, as the matcher compares it).
    assert data["fields"]["cnic"] == {"value": "4210112345671", "confidence": "high"}
    assert data["fields"]["dob"] == {"value": "1990-08-05", "confidence": "high"}


def test_offer_letter_schema_via_short_form_label(fake_ocr):
    fake_ocr["text"] = OFFER_TEXT
    resp = _extract("offer letter")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["document_type"] == "offer_letter"
    assert set(data["fields"]) == {"name", "designation", "joining_date"}
    assert data["fields"]["name"] == {"value": "Asim Khan", "confidence": "high"}
    assert data["fields"]["designation"] == {
        "value": "Software Engineer",
        "confidence": "high",
    }
    assert data["fields"]["joining_date"] == {
        "value": "2023-06-01",
        "confidence": "high",
    }


def test_missing_required_fields_reported_not_visible(fake_ocr):
    fake_ocr["text"] = "no identity content present at all"
    data = _extract("CNIC / National ID Copy").json()["data"]
    assert data["document_type"] == "cnic"
    assert data["fields"]["name"] == {"value": None, "confidence": "not_visible"}
    assert data["fields"]["dob"] == {"value": None, "confidence": "not_visible"}


def test_unknown_document_type_falls_back_to_generic_schema(fake_ocr):
    data = _extract("some totally unknown type").json()["data"]
    assert data["document_type"] == "generic"
    assert set(data["fields"]) == {"name", "cnic"}