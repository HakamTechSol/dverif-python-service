"""Endpoint tests for POST /validate via FastAPI TestClient (httpx)."""

import io

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.utils.file_handling import upload_size_limit_bytes

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}
LIMIT = upload_size_limit_bytes()


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_valid_text_file_accepted():
    resp = client.post(
        "/validate",
        files={"file": ("ok.txt", b"hello world", "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"valid": True, "reason": None, "file_type": "text"}


def test_valid_png_accepted():
    resp = client.post(
        "/validate",
        files={"file": ("ok.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["valid"] is True


def test_corrupt_pdf_rejected():
    # Sniffs as PDF (magic bytes) but is structurally unreadable -> deep parse
    # fails and the file is reported invalid, not silently accepted.
    broken = b"%PDF-1.7\n" + b"garbage-token-%*@-not-a-pdf " * 60
    resp = client.post(
        "/validate",
        files={"file": ("corrupt.pdf", io.BytesIO(broken), "application/pdf")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["valid"] is False
    assert data["file_type"] == "pdf"


def test_truncated_png_rejected():
    # Valid PNG cut before its IEND trailer -> Pillow verify()/load() fail.
    truncated = _png_bytes()[:-12]
    resp = client.post(
        "/validate",
        files={"file": ("truncated.png", io.BytesIO(truncated), "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["valid"] is False
    assert "corrupt or truncated" in data["reason"]


def test_mismatched_extension_rejected():
    # Perfectly valid PNG bytes declared as .pdf -> renamed-file guard trips.
    resp = client.post(
        "/validate",
        files={"file": ("renamed.pdf", io.BytesIO(_png_bytes()), "application/pdf")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["valid"] is False
    assert "File type mismatch" in data["reason"]


def test_unrecognized_binary_rejected():
    resp = client.post(
        "/validate",
        files={"file": ("junk.bin", io.BytesIO(b"\x00\x01\x02\x03secret"), "application/octet-stream")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["valid"] is False


def test_oversized_upload_rejected():
    oversize = b"x" * (LIMIT + 1024)
    resp = client.post(
        "/validate",
        files={"file": ("big.txt", io.BytesIO(oversize), "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 413