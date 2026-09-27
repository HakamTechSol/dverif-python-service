"""Office-format discrimination: a real .docx vs a renamed / generic .zip.

.docx IS a zip container (OOXML is a zip), which is exactly why this file
exists. The allow-list admits ".docx" and then has to PROVE the container really
is a Word document, because the container on its own carries no such promise: a
bare .zip can hold anything at all.

So the question these tests answer is not "does it open as a zip" (it does, for
both) but "does it contain evidence that it is specifically a Word document".
The evidence is `[Content_Types].xml` plus a `word/` entry, and every rejection
below is that check doing its job.
"""

import io
import zipfile

from fastapi.testclient import TestClient

from app.main import app
from app.services.validation_service import validate

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}


# --- builders ---------------------------------------------------------------


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


def _real_docx() -> bytes:
    """A minimal but structurally valid Word document."""
    return _zip_bytes(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "word/document.xml": (
                '<?xml version="1.0"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Certificate</w:t></w:r></w:p></w:body></w:document>"
            ),
        }
    )


def _real_xlsx() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
            "xl/workbook.xml": '<?xml version="1.0"?><workbook/>',
            "xl/worksheets/sheet1.xml": '<?xml version="1.0"?><worksheet/>',
        }
    )


def _real_pptx() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
            "ppt/presentation.xml": '<?xml version="1.0"?><presentation/>',
        }
    )


def _generic_zip() -> bytes:
    """An ordinary archive. No Content_Types, no word/ — just files."""
    return _zip_bytes(
        {
            "readme.txt": "hello",
            "photos/holiday.jpg": "not-really-a-jpeg",
            "payload.sh": "#!/bin/sh\necho pwned",
        }
    )


def _zip_renamed_to_docx() -> bytes:
    """The attack: a generic archive wearing a .docx extension."""
    return _generic_zip()


def _docx_renamed_to_zip() -> bytes:
    """The reverse: a genuine Word file named .zip. Still not an accepted type."""
    return _real_docx()


def _ole2_xls() -> bytes:
    """Old binary Excel magic (OLE2 compound file)."""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128


def _ole2_doc() -> bytes:
    """An OLE2 container that actually carries the WordDocument stream."""
    body = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    return body + "WordDocument".encode("utf-16-le") + b"\x00" * 64


def _ole2_ppt() -> bytes:
    """OLE2 PowerPoint: the other stream name, still not Word."""
    body = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    return body + "PowerPoint Document".encode("utf-16-le") + b"\x00" * 64


def _csv_bytes() -> bytes:
    return b"name,cnic\nAsim Khan,4210112345671\n"


# --- the real .docx must still work -----------------------------------------


def test_real_docx_is_accepted():
    result = validate_bytes(_real_docx(), "letter.docx", DOCX_MIME)
    assert result["valid"] is True, result["reason"]
    assert result["file_type"] == "docx"
    # A clean pass is the structural tier: the hard checks ran and found nothing.
    assert result["check_type"] == "structural"


def test_real_docx_is_accepted_through_the_endpoint():
    data = _post("letter.docx", _real_docx(), DOCX_MIME)
    assert data["valid"] is True
    assert data["file_type"] == "docx"


# --- a generic zip must not pass as a docx ---------------------------------


def test_generic_zip_renamed_to_docx_is_rejected_structurally():
    # This is the case the whole OOXML part check exists for. The bytes open
    # fine as a zip, so anything that only verified "is a zip" would wave it
    # through.
    result = validate_bytes(_zip_renamed_to_docx(), "letter.docx", DOCX_MIME)
    assert result["valid"] is False
    assert result["check_type"] == "structural"
    assert "word" in result["reason"].lower() or "office" in result["reason"].lower()


def test_generic_zip_renamed_to_docx_is_rejected_through_the_endpoint():
    data = _post("letter.docx", _zip_renamed_to_docx(), DOCX_MIME)
    assert data["valid"] is False
    assert data["check_type"] == "structural"


def test_docx_missing_content_types_is_rejected():
    # word/ is present but [Content_Types].xml is not: still not a Word package.
    result = validate_bytes(
        _zip_bytes({"word/document.xml": "<w:document/>"}), "letter.docx", DOCX_MIME
    )
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_docx_with_content_types_but_no_word_content_is_rejected():
    # The mirror image: the OOXML marker exists but there is no word/ payload.
    result = validate_bytes(
        _zip_bytes({"[Content_Types].xml": "<Types/>", "other/thing.xml": "x"}),
        "letter.docx",
        DOCX_MIME,
    )
    assert result["valid"] is False
    assert result["check_type"] == "structural"


# --- bare zips and Excel must not slip through the Office path --------------


def test_bare_zip_is_rejected():
    result = validate_bytes(_generic_zip(), "bundle.zip", "application/zip")
    assert result["valid"] is False
    assert result["check_type"] == "structural"
    assert "mismatch" in result["reason"].lower()


def test_bare_zip_is_rejected_even_under_the_octet_stream_mime():
    result = validate_bytes(_generic_zip(), "bundle.zip", "application/octet-stream")
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_real_xlsx_is_rejected():
    # A perfectly valid Excel file — Excel simply is not a supported document
    # type, so the extension guard turns it away before the Office part check
    # would even be consulted.
    result = validate_bytes(_real_xlsx(), "sheet.xlsx", XLSX_MIME)
    assert result["valid"] is False
    assert result["check_type"] == "structural"
    assert "mismatch" in result["reason"].lower()


def test_real_xlsx_renamed_to_docx_is_rejected():
    # Contains xl/, not word/ — so even under a .docx name the part check
    # refuses it. This is what stops format-substitution between Office types.
    result = validate_bytes(_real_xlsx(), "letter.docx", DOCX_MIME)
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_real_pptx_is_rejected():
    result = validate_bytes(_real_pptx(), "deck.pptx", PPTX_MIME)
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_real_pptx_renamed_to_docx_is_rejected():
    result = validate_bytes(_real_pptx(), "letter.docx", DOCX_MIME)
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_ole2_xls_is_rejected():
    # OLE2 magic, .xls name. Only legacy .doc is accepted from this family.
    result = validate_bytes(_ole2_xls(), "sheet.xls", "application/vnd.ms-excel")
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_ole2_xls_renamed_to_doc_is_rejected():
    # A .doc name does not make an OLE2 spreadsheet a Word document. OLE2 magic
    # is shared by .doc/.xls/.ppt, so without the WordDocument stream check this
    # would be accepted as a Word file.
    result = validate_bytes(_ole2_xls(), "letter.doc", "application/msword")
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_ole2_powerpoint_renamed_to_doc_is_rejected():
    # Same story via the other sibling stream name.
    result = validate_bytes(_ole2_ppt(), "letter.doc", "application/msword")
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_real_legacy_doc_is_still_accepted():
    # The counterweight: tightening the OLE2 branch must not break the format it
    # was meant to allow. A container with the WordDocument stream and a .doc
    # name is a legacy Word document.
    result = validate_bytes(_ole2_doc(), "letter.doc", "application/msword")
    assert result["valid"] is True
    assert result["file_type"] == "doc"
    assert result["check_type"] == "structural"


def test_csv_is_rejected():
    result = validate_bytes(_csv_bytes(), "data.csv", "text/csv")
    assert result["valid"] is False
    assert result["check_type"] == "structural"


def test_csv_renamed_to_txt_is_accepted():
    # The counterweight: the same bytes under a supported extension are fine.
    # Only the .csv name is refused, not the content.
    result = validate_bytes(_csv_bytes(), "notes.txt", "text/plain")
    assert result["valid"] is True


# --- helpers ----------------------------------------------------------------

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS_MIME = "application/vnd.ms-excel"
PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)


def validate_bytes(payload: bytes, filename: str, mimetype: str) -> dict:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(payload)
        return validate(str(path), filename, mimetype)


def _post(filename: str, payload: bytes, mimetype: str) -> dict:
    resp = client.post(
        "/validate",
        files={"file": (filename, io.BytesIO(payload), mimetype)},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]
