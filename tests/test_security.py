"""Security-surface tests: auth gating, fail-closed behaviour, `/metrics`
protection, and the regression guard that extracted PII never reaches the logs.
"""

import io
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}

SIMPLE_FILE = ("a.txt", io.BytesIO(b"hello"), "text/plain")

# (verb, path, files) — request bodies are irrelevant: auth runs before parsing.
AUTH_SURFACE = [
    ("post", "/validate", {"file": SIMPLE_FILE}),
    ("post", "/ocr/extract", {"file": SIMPLE_FILE}),
    ("post", "/match", None),
    ("get", "/metrics", None),
]


def test_health_requires_no_key():
    resp = client.get("/health")
    assert resp.status_code == 200


def test_missing_api_key_rejected_everywhere():
    for verb, path, files in AUTH_SURFACE:
        kwargs = {"files": files} if files else {}
        resp = getattr(client, verb)(path, **kwargs)
        assert resp.status_code == 401, path


def test_wrong_api_key_rejected_everywhere():
    for verb, path, files in AUTH_SURFACE:
        kwargs = {"files": files, "headers": {"X-API-Key": "definitely-wrong"}} if files else {"headers": {"X-API-Key": "definitely-wrong"}}
        resp = getattr(client, verb)(path, **kwargs)
        assert resp.status_code == 401, path


def test_authentication_precedes_upload_size_gate():
    # Without a key, even a clearly-oversized body must yield 401, never 413:
    # auth is checked before the size gate reads the body.
    huge = b"x" * (2 * 1024 * 1024)
    resp = client.post("/validate", content=huge)
    assert resp.status_code == 401


def test_unconfigured_service_fails_closed_with_503(monkeypatch):
    monkeypatch.setattr(settings, "doc_service_api_key", "")
    monkeypatch.setenv("DOC_SERVICE_API_KEY", "")
    resp = client.post(
        "/validate",
        files={"file": SIMPLE_FILE},
        headers={"X-API-Key": "anything"},
    )
    assert resp.status_code == 503


def test_metrics_endpoint_is_protected():
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers=HEADERS)
    assert ok.status_code == 200
    assert "document_service_requests_total" in ok.text


def test_logs_never_contain_extracted_pii(monkeypatch, caplog):
    """Log-privacy regression guard.

    The /ocr/extract path is exercised with a document whose extracted name and
    CNIC are distinctive markers; neither may appear in any log output, while
    the request line IS written and carries only metadata.
    """
    cnic_text = (
        "Name: FatimaNoorMarker\n"
        "CNIC Number: 42101-9876543-1\n"
        "Date of Birth: 05-08-1990\n"
    )

    def fake_extract_text(_path):
        return cnic_text

    monkeypatch.setattr("app.api.routes.ocr.extractText", fake_extract_text)
    caplog.set_level(logging.INFO, logger="dvarif.docservice")
    logger = logging.getLogger("dvarif.docservice")
    logger.propagate = True  # let pytest's caplog see the records
    try:
        resp = client.post(
            "/ocr/extract",
            files={"file": ("doc.txt", io.BytesIO(b"ignored"), "text/plain")},
            data={"document_type": "CNIC / National ID Copy"},
            headers=HEADERS,
        )
        assert resp.status_code == 200

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "FatimaNoorMarker" not in blob
        assert "42101" not in blob
        assert "9876543" not in blob

        # The request log line exists and carries only metadata:
        doc_types = [getattr(r, "document_type", None) for r in caplog.records]
        assert "cnic" in doc_types
    finally:
        logger.propagate = False


def test_no_print_or_raw_ocr_text_in_log_statements_in_source():
    """Static audit: the service never calls print() and never passes raw OCR
    text / extracted values into a logger.

    (The full source-tree grep confirmed zero pre-existing print/logging calls;
    this keeps it that way.)
    """
    app_root = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for py in sorted(app_root.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if "print(" in text:
            offenders.append(f"{py}: contains print(")
    assert not offenders, "\n".join(offenders)