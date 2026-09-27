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

Every verdict is additionally tagged with a ``check_type`` tier so callers can
tell an unarguable failure from a debatable one:

  - ``structural`` -- the bytes themselves are unacceptable: a parser refuses
    the file, it is empty/truncated, or the declared extension/mimetype
    contradicts the sniffed content (MIME spoofing). Always block these; the
    spoofing check in particular is a security control, not a quality knob.
  - ``heuristic`` -- a subjective quality signal (crop detection, the
    darkness/low-contrast readability check). These are tuning-sensitive and can
    produce false positives on genuinely good scans, so they are surfaced for a
    human rather than treated as disqualifying.
  - a clean pass also reports ``structural``, meaning the hard checks ran and
    found nothing.
"""


import zipfile
from pathlib import Path

try:
    import pymupdf as fitz  # newer PyMuPDF package name
except ImportError:  # pragma: no cover - older installs
    import fitz  # type: ignore
from PIL import Image

from app.services.crop_detection import detect_crop

MAGIC_PDF = b"%PDF-"
MAGIC_JPEG = b"\xff\xd8\xff"
MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_ZIP = b"PK\x03\x04"
MAGIC_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
OOXML_EXTENSIONS = {".docx", ".xlsx", ".pptx"}

# Which filename extensions are allowed for each sniffed type.
#
# The zip family is the subtle one. A .docx IS a zip (OOXML is a zip container),
# so the tempting shortcut is to also allow .zip / .xlsx / .pptx. That is what
# makes this table exist: the allow-list names the DOCUMENT TYPE, not the
# container. ".docx" is admitted here and then PROVEN by the required-parts
# check below ([Content_Types].xml + a word/ entry), whereas a bare .zip carries
# no such evidence and can contain anything at all. Excel is simply not a
# supported document type.
EXPECTED_EXTENSIONS = {
    "pdf": {".pdf"},
    "jpeg": {".jpg", ".jpeg"},
    "png": {".png"},
    "zip": {".docx"},
    "ole2": {".doc"},
    "text": {".txt"},
}

# Declared mimetype -> the sniffed type it legitimately implies.
#
# This is a spoofing guard, not a content-type validator, so it is deliberately
# a short allow-list of *unambiguous* types. A mimetype that is absent, generic
# ("application/octet-stream") or simply not listed here raises no complaint —
# far too many ordinary clients label a perfectly good PDF as octet-stream, and
# blocking those would be a false positive. What matters is a CONTRADICTION: a
# declared type that positively implies a different format than the bytes
# actually contain (e.g. HTML content announced as application/pdf).
MIMETYPE_IMPLIES = {
    "application/pdf": "pdf",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/pjpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/webp": "image",
    "image/bmp": "image",
    "image/tiff": "image",
    "text/html": "text",
    "application/xhtml+xml": "text",
    "text/csv": "text",
    "text/plain": "text",
}

# Min. required ZIP entries for each Office format.
#
# Only .docx is reachable: EXPECTED_EXTENSIONS maps sniffed "zip" content to
# .docx alone, so .xlsx/.pptx are rejected by the extension guard before
# parse_zip is ever called with them. They are kept listed so that if a format
# is ever re-admitted, the part check that actually proves it is a real document
# is already in place rather than being invented under pressure.
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


# The OLE2 compound-file signature is shared by legacy .doc, .xls AND .ppt, so
# magic bytes alone cannot tell them apart — which means a renamed spreadsheet is
# indistinguishable from a word-processor document by signature. The OLE2
# directory stores its entry names as UTF-16LE, and the stdlib has no compound
# file parser, so the one cheap discriminator available is the presence of the
# "WordDocument" stream, which only .doc has (.xls uses Workbook/Book, .ppt uses
# "PowerPoint Document"). Without it we cannot honestly claim the file is a Word
# document, and accepting it is exactly the format-substitution this check
# exists to prevent.
WORD_OLE2_STREAM = "WordDocument"


def is_word_ole2(file_path: str) -> bool:
    """True if the OLE2 container carries the WordDocument stream."""
    try:
        with open(file_path, "rb") as fh:
            head = fh.read(1 << 20)  # the directory lives near the front
    except OSError:
        return False
    return WORD_OLE2_STREAM.encode("utf-16-le") in head


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


CHECK_STRUCTURAL = "structural"
CHECK_HEURISTIC = "heuristic"


def _result(
    valid: bool,
    reason: str | None,
    file_type: str | None,
    file_path: str,
    check_type: str,
) -> dict:
    """Build the /validate payload, appending the crop verdict.

    `check_type` is a REQUIRED argument on purpose. Every call site has to name
    the tier it is claiming, so a parse failure can never be quietly filed as a
    heuristic one and a quality signal can never be filed as a hard failure by
    accident. It is not a defaulted parameter for exactly that reason.

    `valid=True` means only "this file parses". Quality is a SEPARATE axis, so a
    structurally sound file that the crop/darkness heuristic dislikes comes back
    with ``valid=False, check_type="heuristic"`` — never as a silent pass.

    That inversion is the whole point of the two tiers. Previously a cropped scan
    reported ``valid=True, cropped=True`` and every client had to special-case
    the `cropped` field to notice it. Now the verdict is self-describing: a
    caller that only reads `valid` blocks both cases, and a caller that reads
    `check_type` can tell a hard parse failure from a debatable quality call
    without knowing anything about cropping.

    Crop detection only runs for structurally sound files — a corrupt document
    has no meaningful frame to measure, so the corruption verdict is reported
    on its own and the crop fields are neutral.
    """
    if not valid:
        return {
            "valid": False,
            "reason": reason,
            "file_type": file_type,
            "check_type": check_type,
            "cropped": False,
            "crop_reason": None,
            "crop_score": 0.0,
        }

    crop = detect_crop(file_path, file_type)
    if crop["cropped"]:
        # Quality heuristic fired on a file that parses fine. `reason` carries the
        # detector's message so callers have one canonical string to store or
        # show, and `crop_reason` is kept for clients that already read it.
        return {
            "valid": False,
            "reason": crop["reason"],
            "file_type": file_type,
            "check_type": CHECK_HEURISTIC,
            "cropped": True,
            "crop_reason": crop["reason"],
            "crop_score": crop["score"],
        }

    return {
        "valid": True,
        "reason": None,
        "file_type": file_type,
        "check_type": CHECK_STRUCTURAL,
        "cropped": False,
        "crop_reason": None,
        "crop_score": 0.0,
    }


def validate(file_path: str, filename: str | None = None, mimetype: str | None = None) -> dict:
    """Return {valid, reason, file_type, check_type, cropped, crop_reason, crop_score}.

    `check_type` is "structural" for every hard parse/mimetype failure and for
    a clean pass, and "heuristic" when a quality signal (crop, darkness) is what
    made the file invalid. See the module docstring.
    """
    filename = filename or Path(file_path).name
    ext = Path(filename).suffix.lower()

    if not Path(file_path).exists() or Path(file_path).stat().st_size == 0:
        return _result(False, "File is empty or missing", None, file_path, CHECK_STRUCTURAL)

    detected = sniff(file_path)

    # Renamed-file guard: a file whose declared extension contradicts its
    # actual content is a red flag even if the bytes parse cleanly.
    if detected and ext not in EXPECTED_EXTENSIONS.get(detected, {}):
        return _result(
            False,
            f"File type mismatch: declared '{ext}' but content is {detected.upper()}",
            detected,
            file_path,
            CHECK_STRUCTURAL,
        )

    # Mimetype spoofing guard: the declared type positively implies a different
    # format than the bytes contain (HTML announced as application/pdf, a PDF
    # announced as image/png, ...). This is a SECURITY control and is therefore
    # always reported as structural, so it can never be softened into a
    # heuristic. Unknown/generic mimetypes are exempt (see MIMETYPE_IMPLIES).
    if detected and mimetype:
        declared = mimetype.split(";")[0].strip().lower()
        implied = MIMETYPE_IMPLIES.get(declared)
        # Both sides are compared as FORMAT FAMILIES: every raster image is
        # "image", so a PNG announced as image/jpeg is not flagged. Decoding is
        # driven by the sniffed bytes either way, so mislabelling one image
        # format as another is a cosmetic problem, not a security one. The
        # dangerous cases — text/script content announced as a document, or a
        # document announced as an image — still cross families and still block.
        detected_family = "image" if detected in ("jpeg", "png") else detected
        if implied and implied != detected_family:
            return _result(
                False,
                (
                    f"File type mismatch: declared content type '{declared}' but "
                    f"content is {detected.upper()}"
                ),
                detected,
                file_path,
                CHECK_STRUCTURAL,
            )

    # Every branch below is a hard fact about the bytes: a library refused to
    # open the file, or the format is not one we accept. All structural.
    if detected == "pdf":
        ok, reason = parse_pdf(file_path)
        return _result(ok, reason, "pdf", file_path, CHECK_STRUCTURAL)
    if detected in ("jpeg", "png"):
        ok, reason = parse_image(file_path)
        return _result(ok, reason, detected, file_path, CHECK_STRUCTURAL)
    if detected == "zip":
        ok, reason, file_type = parse_zip(file_path, ext)
        return _result(ok, reason, file_type, file_path, CHECK_STRUCTURAL)
    if detected == "ole2":
        # Old binary Office. The extension guard above has already restricted this
        # branch to .doc; what it could not do is prove the OLE2 container really
        # is a Word document, since .xls and .ppt share the same magic.
        if not is_word_ole2(file_path):
            return _result(
                False,
                "Legacy Office file is not a Word document (no WordDocument stream)",
                "ole2",
                file_path,
                CHECK_STRUCTURAL,
            )
        return _result(True, None, "doc", file_path, CHECK_STRUCTURAL)
    if detected == "text":
        return _result(True, None, "text", file_path, CHECK_STRUCTURAL)

    # Unknown magic: Pillow fallback for any image-looking extension.
    if ext in IMAGE_EXTENSIONS:
        ok, reason = parse_image(file_path)
        if ok:
            return _result(True, None, "image", file_path, CHECK_STRUCTURAL)
        return _result(False, reason, "image", file_path, CHECK_STRUCTURAL)

    return _result(
        False,
        "Unrecognized file type (expected PDF, JPEG, PNG, ZIP/DOCX/XLSX, Office or text)",
        None,
        file_path,
        CHECK_STRUCTURAL,
    )