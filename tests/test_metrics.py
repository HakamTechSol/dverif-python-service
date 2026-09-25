"""Tests for GET /metrics: auth, per-endpoint counters, and the per-document-
type outcome tallies that power approval vs fallback analytics."""

import io

from fastapi.testclient import TestClient

from app.core import metrics
from app.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}


def test_metrics_requires_api_key():
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=HEADERS).status_code == 200


def test_per_endpoint_request_and_latency_counters():
    metrics.reset()
    client.post(
        "/validate",
        files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=HEADERS,
    )
    body = client.get("/metrics", headers=HEADERS).text
    assert 'document_service_requests_total{endpoint="/validate"} 1' in body
    assert 'document_service_request_duration_seconds_count{endpoint="/validate"} 1' in body
    assert 'document_service_request_duration_seconds_sum{endpoint="/validate"}' in body


def test_failures_are_counted_separately():
    metrics.reset()
    client.post("/validate", files={"file": ("a.txt", b"no key", "text/plain")})  # 401
    client.post(
        "/validate",
        files={"file": ("a.txt", b"wrong key", "text/plain")},
        headers={"X-API-Key": "wrong"},
    )  # 401
    client.post(
        "/validate",
        files={"file": ("a.txt", b"ok", "text/plain")},
        headers=HEADERS,
    )  # 200
    body = client.get("/metrics", headers=HEADERS).text
    assert 'document_service_requests_total{endpoint="/validate"} 3' in body
    assert 'document_service_failures_total{endpoint="/validate"} 2' in body


def test_match_outcome_recorded_by_document_type(monkeypatch):
    metrics.reset()

    def fake_extract_text(path):
        with open(path, "rb") as fh:
            body = fh.read()
        return "Name: Asim Khan\nCNIC Number: 42101-1234567-1\nDate of Birth: 05-08-1990\n"

    monkeypatch.setattr("app.services.match_service.extractText", fake_extract_text)

    resp = client.post(
        "/match",
        files={
            "file_a": ("a.txt", io.BytesIO(b"A"), "text/plain"),
            "file_b": ("b.txt", io.BytesIO(b"B"), "text/plain"),
        },
        data={
            "document_type_a": "CNIC / National ID Copy",
            "document_type_b": "CNIC / National ID Copy",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200

    body = client.get("/metrics", headers=HEADERS).text
    assert (
        'document_service_outcome_total{endpoint="/match",document_type="cnic",outcome="match"} 1'
        in body
    )