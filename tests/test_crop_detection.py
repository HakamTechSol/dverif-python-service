"""Cropped-document detection tests (image edge-ink + PDF content overflow)."""

import io

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app
from app.services.crop_detection import detect_crop, detect_crop_image, detect_crop_pdf

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}


def _clean_png() -> bytes:
    """A page on white paper with a comfortable white margin on all sides."""
    img = Image.new("RGB", (800, 1000), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, 740, 940], outline="black", width=3)
    d.text((120, 200), "CERTIFICATE OF EMPLOYMENT", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _cropped_png() -> bytes:
    """Content runs off the top and left edges — the page was cut through."""
    img = Image.new("RGB", (800, 1000), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 740, 940], outline="black", width=8)
    d.text((30, 4), "CERTIFICATE", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --- image detector ---------------------------------------------------------


def test_clean_image_not_flagged(tmp_path):
    path = tmp_path / "clean.png"
    path.write_bytes(_clean_png())
    result = detect_crop_image(str(path))
    assert result["cropped"] is False
    assert result["reason"] is None


def test_cropped_image_flagged(tmp_path):
    path = tmp_path / "cropped.png"
    path.write_bytes(_cropped_png())
    result = detect_crop_image(str(path))
    assert result["cropped"] is True
    assert result["score"] > 0
    assert "cropped" in result["reason"].lower()


def test_tiny_image_is_not_judged(tmp_path):
    # A thumbnail always has ink on its border; we refuse to guess rather than
    # reject a legitimate small file.
    img = Image.new("RGB", (20, 20), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 19, 19], outline="black", width=3)
    path = tmp_path / "tiny.png"
    img.save(path)
    assert detect_crop_image(str(path))["cropped"] is False


def test_detect_crop_dispatches_on_sniffed_type(tmp_path):
    path = tmp_path / "clean.png"
    path.write_bytes(_clean_png())
    assert detect_crop(str(path), "png")["cropped"] is False
    # Text/office formats have no frame concept.
    assert detect_crop(str(path), "text")["cropped"] is False


# --- pdf detector -----------------------------------------------------------


def _clean_pdf(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "CERTIFICATE OF EMPLOYMENT", fontsize=18)
    page.insert_text((72, 140), "This certifies that Asad Khan is employed here.", fontsize=11)
    path = tmp_path / "clean.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_clean_pdf_not_flagged(tmp_path):
    assert detect_crop_pdf(str(_clean_pdf(tmp_path)))["cropped"] is False


def test_pdf_with_content_beyond_page_flagged(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    # Drawn well outside the visible page rect, i.e. clipped by a crop operation.
    page.draw_rect(fitz.Rect(-120, -120, 400, 300), color=(0, 0, 0), width=3)
    page.insert_text((60, 200), "PARTIALLY CUT", fontsize=14)
    path = tmp_path / "cropped.pdf"
    doc.save(str(path))
    doc.close()

    result = detect_crop_pdf(str(path))
    assert result["cropped"] is True
    assert result["score"] > 1.0


# --- endpoint ---------------------------------------------------------------


def test_validate_endpoint_reports_cropped_image():
    resp = client.post(
        "/validate",
        files={"file": ("cropped.png", io.BytesIO(_cropped_png()), "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Structurally fine, but rejected for quality at the caller's discretion.
    assert data["valid"] is True
    assert data["cropped"] is True
    assert data["crop_reason"]


def test_validate_endpoint_reports_clean_image():
    resp = client.post(
        "/validate",
        files={"file": ("clean.png", io.BytesIO(_clean_png()), "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["cropped"] is False
