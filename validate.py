"""
Real corrupt-file detection for the Dvarif document service.

Strategy — two layers of defence:

1. Magic-byte sniffing: the first bytes of the file must match a known
   signature (PDF, JPEG, PNG, ZIP/OLE2) or be plain printable text.
   This catches renamed/garbage files before any parsing.
2. Structural parse: actually open the file with the appropriate library
   and force a full read so truncation and CRC failures surface:
   - PDF   -> PyMuPDF (fitz) open + page count + trailing %%EOF marker
   - JPEG/PNG -> Pillow verify() + full decode via load()
   - ZIP/OOXML (zip, docx, xlsx, pptx) -> zipfile.testzip() CRC check +
     required package parts for Office documents
"""

import zipfile
from pathlib import Path

try:
    import pymupdf as fitz  # newer PyMuPDF package name
except ImportError:  # pragma: no cover - older installs
    import fitz  # type: ignore
from PIL import Image

MAGIC_PDF = b"%PDF-"
MAGIC_JPEG = b"\xff\xd8\xff"
MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_ZIP = b"PK\x03\x04"
MAGIC_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
OOXML_EXTENSIONS = {".docx", ".xlsx", ".pptx"}

# Which filename extensions are allowed for each sniffed type.
EXPECTED_EXTENSIONS = {
    "pdf": {".pdf"},
    "jpeg": {".jpg", ".jpeg"},
    "png": {".png"},
    "zip": {".zip", ".docx", ".xlsx", ".pptx"},
    "ole2": {".doc", ".xls", ".ppt"},
    "text": {".txt", ".csv"},
}

# Min. required ZIP entries for each Office format.
OOXML_REQUIRED = {
    ".docx": {"[Content_Types].xml", "word/"},
    ".xlsx": {"[Content_Types].xml", "xl/"},
    ".pptx": {"[Content_Types].xml", "ppt/"},
}


def sniff(file_path: str) -> str | None:
    """Classify a file by its magic bytes (pdf|jpeg|png|zip|ole2|text|None)."""
    with open(file_path, "rb") as fh:
        head = fh.read(8)

    if head.startswith(MAGIC_PDF):
        return "pdf"
    if head.startswith(MAGIC_JPEG):
        return "jpeg"
    if head.startswith(MAGIC_PNG):
        return "png"
    if head.startswith(MAGIC_ZIP):
        return "zip"
    if head.startswith(MAGIC_OLE2):
        return "ole2"

    # Plain-text heuristic: no NUL bytes, mostly printable ASCII.
    with open(file_path, "rb") as fh:
        sample = fh.read(512)
    if b"\x00" not in sample and sample:
        printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
        if printable / len(sample) >= 0.9:
            return "text"
    return None


def has_eof_marker(file_path: str) -> bool:
    """True if the last ~2KB of the PDF contain %%EOF (truncation check)."""
    with open(file_path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 2048))
        tail = fh.read()
    return b"%%EOF" in tail


def parse_pdf(file_path: str) -> tuple[bool, str | None]:
    try:
        doc = fitz.open(file_path)
    except Exception as exc:
        return False, f"PDF cannot be parsed: {exc}"
    try:
        count = doc.page_count
    finally:
        doc.close()
    if count <= 0:
        return False, "PDF contains no renderable pages"
    if not has_eof_marker(file_path):
        return False, "PDF is truncated (missing %%EOF marker)"
    return True, None


def parse_image(file_path: str) -> tuple[bool, str | None]:
    try:
        with Image.open(file_path) as im:
            im.verify()
    except Exception as exc:
        return False, f"Image is corrupt or truncated: {exc}"
    # verify() drains the file handle and may skip full decoding; reopen and
    # force a complete decode so partial/truncated images throw.
    try:
        with Image.open(file_path) as im:
            im.load()
    except Exception as exc:
        return False, f"Image is corrupt or truncated: {exc}"
    return True, None


def parse_zip(file_path: str, ext: str) -> tuple[bool, str | None, str]:
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = archive.namelist()
            if not names:
                return False, "ZIP archive is empty", "zip"
            bad = archive.testzip()
            if bad is not None:
                return False, f"ZIP archive is corrupt (bad entry: {bad})", "zip"
            if ext in OOXML_EXTENSIONS:
                name_set = set(names)
                fixed = {entry for entry in OOXML_REQUIRED[ext] if not entry.endswith("/")}
                missing = fixed - name_set
                if missing:
                    return False, f"Not a valid Office document: missing {sorted(missing)}", "zip"
                prefix = next(entry for entry in OOXML_REQUIRED[ext] if entry.endswith("/"))
                if not any(n.startswith(prefix) for n in names):
                    return False, f"Not a valid Office document: no {prefix} content", "zip"
                return True, None, ext.lstrip(".")
            return True, None, "zip"
    except zipfile.BadZipFile as exc:
        return False, f"Not a valid ZIP archive: {exc}", "zip"
    except Exception as exc:
        return False, f"ZIP archive cannot be read: {exc}", "zip"


def validate(file_path: str, filename: str | None = None) -> dict:
    """Return {valid, reason, file_type} for the given file."""
    filename = filename or Path(file_path).name
    ext = Path(filename).suffix.lower()

    if not Path(file_path).exists() or Path(file_path).stat().st_size == 0:
        return {"valid": False, "reason": "File is empty or missing", "file_type": None}

    detected = sniff(file_path)

    # Renamed-file guard: a file whose declared extension contradicts its
    # actual content is a red flag even if the bytes parse cleanly.
    if detected and ext not in EXPECTED_EXTENSIONS.get(detected, {}):
        return {
            "valid": False,
            "reason": f"File type mismatch: declared '{ext}' but content is {detected.upper()}",
            "file_type": detected,
        }

    if detected == "pdf":
        ok, reason = parse_pdf(file_path)
        return {"valid": ok, "reason": reason, "file_type": "pdf"}
    if detected in ("jpeg", "png"):
        ok, reason = parse_image(file_path)
        return {"valid": ok, "reason": reason, "file_type": detected}
    if detected == "zip":
        ok, reason, file_type = parse_zip(file_path, ext)
        return {"valid": ok, "reason": reason, "file_type": file_type}
    if detected == "ole2":
        # Old binary Office (.doc/.xls/.ppt) — magic matches; no cheap deep
        # parse in stdlib/Pillow, so accept on signature alone.
        return {"valid": True, "reason": None, "file_type": "ole2"}
    if detected == "text":
        return {"valid": True, "reason": None, "file_type": "text"}

    # Unknown magic: Pillow fallback for any image-looking extension.
    if ext in IMAGE_EXTENSIONS:
        ok, reason = parse_image(file_path)
        if ok:
            return {"valid": True, "reason": None, "file_type": "image"}
        return {"valid": False, "reason": reason, "file_type": "image"}

    return {
        "valid": False,
        "reason": "Unrecognized file type (expected PDF, JPEG, PNG, ZIP/DOCX/XLSX, Office or text)",
        "file_type": None,
    }