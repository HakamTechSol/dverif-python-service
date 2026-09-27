"""Tests for the readability pre-checks and DOCX text extraction.

The crop detector's edge-ink test assumes a bright sheet of paper. On an
under-exposed capture that assumption fails and the verdict blames an arbitrary
edge, so these tests pin the two behaviours that fix: a too-dark frame reports
the lighting problem, and a genuinely cropped frame is still reported as cropped.
"""

import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app
from app.services.crop_detection import detect_crop, detect_crop_image
from app.services.ocr_service import extractFields, extractText
from app.services.validation_service import validate

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}


# --- fixtures ---------------------------------------------------------------


def _under_exposed_png() -> bytes:
    """A capture with no paper-white anywhere: brightest pixel is ~214/255.

    Modelled on a real rejected upload (1200x1600, mean 114, max 214). Every
    pixel is 'ink' by the edge test, so without the readability pre-check this
    is reported as a crop on an arbitrary edge.
    """
    img = Image.new("RGB", (1200, 1600), (140, 140, 142))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, 1140, 1540], outline=(30, 30, 30), width=4)
    d.text((200, 400), "CERTIFICATE OF EMPLOYMENT", fill=(20, 20, 20))
    d.text((200, 470), "Name: Asad Khan", fill=(25, 25, 25))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _normal_photo_png() -> bytes:
    """A slightly off-white photo of a page: must NOT be flagged."""
    img = Image.new("RGB", (1200, 1600), (238, 238, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, 1140, 1540], outline=(120, 120, 120), width=3)
    d.text((200, 400), "CERTIFICATE OF EMPLOYMENT", fill=(60, 60, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _genuinely_cropped_png() -> bytes:
    img = Image.new("RGB", (800, 1000), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 740, 940], outline="black", width=8)
    d.text((30, 4), "CERTIFICATE", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --- readability pre-checks -------------------------------------------------


def test_under_exposed_image_reports_lighting_not_crop(tmp_path):
    path = tmp_path / "dark.png"
    path.write_bytes(_under_exposed_png())
    result = detect_crop_image(str(path))

    assert result["cropped"] is True
    reason = result["reason"].lower()
    # The whole point: it must NOT blame an edge any more.
    assert "runs off the" not in reason
    assert "under-exposed" in reason or "too dark" in reason
    # And it must tell the user what to actually do.
    assert "re-scan" in reason


def test_under_exposed_image_keeps_the_brightest_pixel_as_evidence(tmp_path):
    path = tmp_path / "dark.png"
    path.write_bytes(_under_exposed_png())
    reason = detect_crop_image(str(path))["reason"]
    # The message cites the measurement, so a support ticket is diagnosable.
    assert "214" in reason or "brightest pixel" in reason


def test_normal_photo_of_a_page_is_not_flagged(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(_normal_photo_png())
    result = detect_crop_image(str(path))
    assert result["cropped"] is False
    assert result["reason"] is None


def test_a_genuine_crop_is_still_reported_as_a_crop(tmp_path):
    """The readability pre-check must not swallow the real thing it guards."""
    path = tmp_path / "cropped.png"
    path.write_bytes(_genuinely_cropped_png())
    result = detect_crop_image(str(path))
    assert result["cropped"] is True
    assert "cropped" in result["reason"].lower()
    assert "runs off the" in result["reason"]


def test_validate_endpoint_surfaces_the_dark_scan_reason():
    resp = client.post(
        "/validate",
        files={"file": ("dark.png", io.BytesIO(_under_exposed_png()), "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Structurally fine -- it is a readable PNG, just badly lit. The lighting
    # problem is a quality judgement, so it lands in the heuristic tier: the
    # file is marked invalid, but a caller can tell this is a maybe, not a
    # certainty, and choose to warn instead of hard-blocking.
    assert data["check_type"] == "heuristic"
    assert data["valid"] is False
    assert data["cropped"] is True
    assert "under-exposed" in data["crop_reason"].lower()
    assert data["reason"] == data["crop_reason"]


# --- PNG --------------------------------------------------------------------


def test_png_is_accepted_and_routed_to_the_image_validator(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(_normal_photo_png())
    result = validate(str(path), "page.png")
    assert result["valid"] is True
    assert result["file_type"] == "png"
    assert result["cropped"] is False


def test_png_dispatches_to_the_image_crop_detector(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(_normal_photo_png())
    assert detect_crop(str(path), "png")["cropped"] is False


# --- DOCX -------------------------------------------------------------------


def _docx(tmp_path, name="letter.docx"):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("EMPLOYMENT CERTIFICATE")
    d.add_paragraph("Name: Asad Khan")
    d.add_paragraph("CNIC: 35202-1234567-1")
    d.add_paragraph("Designation: Senior Engineer")
    d.add_paragraph("Date of Joining: 12 March 2023")
    path = tmp_path / name
    d.save(str(path))
    return path


def test_docx_is_a_valid_office_package(tmp_path):
    path = _docx(tmp_path)
    result = validate(str(path), "letter.docx")
    assert result["valid"] is True
    # Typed as docx (not a generic zip) so the required word/ parts are enforced.
    assert result["file_type"] == "docx"


def test_docx_is_never_treated_as_a_scannable_frame(tmp_path):
    """A DOCX is reflowable: it has no page edge that content could run off."""
    path = _docx(tmp_path)
    assert detect_crop(str(path), "docx")["cropped"] is False


def test_docx_text_is_extracted_without_ocr(tmp_path):
    path = _docx(tmp_path)
    text = extractText(str(path))
    assert "Asad Khan" in text
    assert "Senior Engineer" in text
    # Nothing rasterised: an OCR path would have produced a text blob with
    # stray glyphs, so the clean, exact strings are the signal here.
    assert "CERTIFICATE" in text.upper()


def test_docx_feeds_the_same_canonical_field_extraction(tmp_path):
    path = _docx(tmp_path)
    fields = extractFields(extractText(str(path)), "employment_letter")["fields"]
    assert fields["name"]["value"] == "Asad Khan"
    assert fields["name"]["confidence"] == "high"
    assert fields["designation"]["value"] == "Senior Engineer"
    # Dates are normalised to ISO exactly as they are for OCR input.
    assert fields["joining_date"]["value"] == "2023-03-12"


def test_docx_cnic_is_extracted(tmp_path):
    path = _docx(tmp_path)
    fields = extractFields(extractText(str(path)))["fields"]
    assert fields["cnic"]["value"] == "3520212345671"


def test_docx_raw_xml_fallback_matches_python_docx(tmp_path):
    """The stdlib reader must agree, so extraction survives a missing python-docx."""
    from app.services.ocr_service import _docx_text_via_xml

    path = _docx(tmp_path)
    assert "Asad Khan" in _docx_text_via_xml(str(path))
    assert "Senior Engineer" in _docx_text_via_xml(str(path))
