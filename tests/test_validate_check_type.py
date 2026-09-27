"""check_type tiering tests for POST /validate.

Two tiers, and the whole point is that they must never be confused:

  structural -- a hard fact about the bytes. A parser refuses the file, it is
                empty or truncated, or the declared extension/mimetype
                contradicts the sniffed content. Always blocks a request.
  heuristic  -- a subjective quality signal (crop detection, darkness /
                low-contrast readability). Tuning-sensitive, can fire on a
                perfectly good scan, so it is a warning rather than a block.

The security-relevant case here is MIME spoofing. It MUST stay in the structural
tier no matter how the tiering is tuned, because softening it would let a file
whose declared type lies about its content become a request.
"""

import io

import fitz  # PyMuPDF
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app
from app.services.validation_service import validate

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}


def _post(filename: str, payload: bytes, mimetype: str) -> dict:
    resp = client.post(
        "/validate",
        files={"file": (filename, io.BytesIO(payload), mimetype)},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# --- fixtures ---------------------------------------------------------------


def _real_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello world")
    out = doc.tobytes()
    doc.close()
    return out


def _truncated_pdf() -> bytes:
    """A PDF whose %%EOF trailer was chopped off — a genuine structural break."""
    full = _real_pdf()
    assert b"%%EOF" in full
    return full[: int(len(full) * 0.6)]


def _cropped_png() -> bytes:
    """Well-lit page content that runs off the left edge — a quality signal."""
    img = Image.new("RGB", (800, 1000), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 740, 940], outline="black", width=8)
    d.text((30, 4), "CERTIFICATE", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _dark_png() -> bytes:
    """Every pixel below the paper threshold — a lighting problem, not a parse one."""
    img = Image.new("RGB", (1200, 1600), (200, 200, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, 1140, 1540], outline=(150, 150, 150), width=4)
    d.text((200, 400), "CERTIFICATE", fill=(140, 140, 140))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _clean_png() -> bytes:
    img = Image.new("RGB", (1000, 1400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([120, 120, 880, 1280], outline="black", width=3)
    d.text((200, 400), "CERTIFICATE", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


HTML_AS_PDF = b"<!DOCTYPE html><html><body><h1>Not a document</h1></body></html>"


# --- structural: must always be invalid, always block ------------------------


def test_truncated_pdf_is_structural():
    data = _post("truncated.pdf", _truncated_pdf(), "application/pdf")
    assert data["valid"] is False
    assert data["check_type"] == "structural"
    assert data["reason"]
    # Structural failures never carry a quality signal: there is no frame to
    # measure on an unparseable file, so the crop fields stay neutral.
    assert data["cropped"] is False
    assert data["crop_reason"] is None


def test_zero_byte_file_is_structural():
    data = _post("empty.pdf", b"", "application/pdf")
    assert data["valid"] is False
    assert data["check_type"] == "structural"


def test_missing_file_is_structural():
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        result = validate(str(Path(td) / "nope.pdf"), "nope.pdf", "application/pdf")
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_html_mislabeled_as_pdf_is_structural_and_not_weakened():
    """The MIME-spoofing case from the security audit.

    HTML bytes, a .pdf filename and an application/pdf mimetype. Two independent
    signals disagree with the content. This must remain a hard block: if it ever
    regressed into the heuristic tier, a file lying about its own type would
    quietly become a reviewable request.
    """
    data = _post("document.pdf", HTML_AS_PDF, "application/pdf")
    assert data["valid"] is False
    assert data["check_type"] == "structural"
    assert "mismatch" in data["reason"].lower()


def test_html_mislabeled_as_pdf_is_caught_by_the_mimetype_alone(tmp_path):
    """Mimetype spoofing must be caught even when the extension is not a giveaway.

    Uses a .txt filename so the extension check has nothing to complain about —
    proving the declared content type is checked in its own right and is not
    merely riding on the extension guard.
    """
    data = _post("notes.txt", HTML_AS_PDF, "application/pdf")
    assert data["valid"] is False
    assert data["check_type"] == "structural"
    assert "application/pdf" in data["reason"]


def test_extension_mismatch_alone_is_structural():
    data = _post("photo.png", _real_pdf(), "image/png")
    assert data["valid"] is False
    assert data["check_type"] == "structural"


def test_pdf_announced_as_an_image_is_structural():
    data = _post("scan.pdf", _real_pdf(), "image/png")
    assert data["valid"] is False
    assert data["check_type"] == "structural"


def test_generic_octet_stream_mimetype_is_not_treated_as_spoofing():
    """A good PDF mislabelled octet-stream must NOT be blocked.

    Far too many ordinary clients send application/octet-stream for a valid
    document. Treating that as spoofing would reject legitimate uploads, which
    is the false-positive failure mode this tiering exists to avoid.
    """
    data = _post("real.pdf", _real_pdf(), "application/octet-stream")
    assert data["valid"] is True
    assert data["check_type"] == "structural"


# --- heuristic: invalid, but explicitly only a "maybe" -----------------------


def test_cropped_image_is_heuristic():
    data = _post("cropped.png", _cropped_png(), "image/png")
    assert data["valid"] is False
    assert data["check_type"] == "heuristic"
    assert data["cropped"] is True
    # One canonical message: `reason` mirrors `crop_reason` so a caller has a
    # single string to persist on the request and show to the reviewer.
    assert data["reason"] == data["crop_reason"]
    assert "runs off the" in data["reason"].lower()


def test_dark_image_is_heuristic_not_structural():
    data = _post("dark.png", _dark_png(), "image/png")
    assert data["valid"] is False
    assert data["check_type"] == "heuristic"
    assert "under-exposed" in data["reason"].lower() or "too dark" in data["reason"].lower()


def test_heuristic_failures_carry_a_real_reason():
    """A soft flag with no explanation is useless to a reviewer."""
    data = _post("cropped.png", _cropped_png(), "image/png")
    assert data["check_type"] == "heuristic"
    assert isinstance(data["reason"], str)
    assert data["reason"].strip()


# --- clean pass --------------------------------------------------------------


def test_clean_image_passes_as_structural():
    data = _post("page.png", _clean_png(), "image/png")
    assert data["valid"] is True
    assert data["check_type"] == "structural"
    assert data["cropped"] is False
    assert data["reason"] is None


def test_check_type_is_always_present_with_one_of_the_two_values():
    """The tier is part of the contract, so it is never absent or a third value."""
    payloads = [
        ("truncated.pdf", _truncated_pdf(), "application/pdf"),
        ("document.pdf", HTML_AS_PDF, "application/pdf"),
        ("cropped.png", _cropped_png(), "image/png"),
        ("dark.png", _dark_png(), "image/png"),
        ("page.png", _clean_png(), "image/png"),
        ("real.pdf", _real_pdf(), "application/pdf"),
    ]
    seen = set()
    for filename, payload, mimetype in payloads:
        data = _post(filename, payload, mimetype)
        assert data["check_type"] in {"structural", "heuristic"}
        seen.add(data["check_type"])
    # Guard against the tiers silently collapsing into one.
    assert seen == {"structural", "heuristic"}
