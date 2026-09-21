"""
OCR text extraction + identity-field extraction for the Dvarif document service.

Pipeline:
  extractText(file)      PDF -> render to image (150 DPI, try page 1, fall
                         back to highest-text-density page) -> Tesseract OCR;
                         images are OCR'd directly.
  extractCnicAndName(t)  regex for CNIC + heuristic name-lookup near
                         "Name"/"\u0646\u0627\u0645" labels, normalized.
"""

import os
import re

try:
    import pymupdf as fitz  # newer PyMuPDF package name
except ImportError:  # pragma: no cover - older installs
    import fitz  # type: ignore
import pytesseract
from PIL import Image

from validate import sniff

# --- Tesseract binary resolution -------------------------------------------


def _resolve_tesseract_cmd():
    """Explicit TESSERACT_CMD > known install paths > rely on PATH."""
    from_env = os.getenv("TESSERACT_CMD")
    if from_env and os.path.exists(from_env):
        return from_env
    known = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for path in known:
        if os.path.exists(path):
            return path
    return None


_cmd = _resolve_tesseract_cmd()
if _cmd:
    pytesseract.pytesseract.tesseract_cmd = _cmd

# --- Regexes ----------------------------------------------------------------

CNIC_RE = re.compile(r"\d{5}[- ]?\d{7}[- ]?\d")
NAME_LABEL_RE = re.compile(r"\b(?:name|\u0646\u0627\u0645)\b", re.IGNORECASE)
OTHER_LABEL_RE = re.compile(
    r"\b(?:c\.?n\.?i\.?c|national\s+identity|identity\s+no\b|"
    r"father|husband|spouse|mother|gender|signature|"
    r"date\b|issue|expiry|\u0634\u0646\u0627\u062e\u062a\u06cc)\b",
    re.IGNORECASE,
)

TESSERACT_LANG = os.getenv("TESSERACT_LANG", "eng")

# --- OCR text extraction ----------------------------------------------------


def _ocr_image(image: Image.Image) -> str:
    try:
        return pytesseract.image_to_string(image, lang=TESSERACT_LANG)
    except pytesseract.TesseractNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "Tesseract OCR binary not found. Install it (see install.ps1) or set "
            "TESSERACT_CMD to the full path of tesseract.exe."
        ) from exc


def _render_page(page, dpi: int = 150) -> Image.Image:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _looks_useful(text: str) -> bool:
    """A page is worth keeping if it has a CNIC or a few words of text."""
    if CNIC_RE.search(text):
        return True
    return len(text.split()) >= 3


def _extract_pdf_text(file_path: str) -> str:
    doc = fitz.open(file_path)
    try:
        pages_to_try = [0]
        if doc.page_count > 1:
            # Fallback candidate: page with the most embedded text.
            best = max(range(doc.page_count), key=lambda i: len(doc[i].get_text()))
            if best != 0:
                pages_to_try.append(best)

        best_text = ""
        for index in pages_to_try:
            text = _ocr_image(_render_page(doc[index]))
            if text and len(text) > len(best_text):
                best_text = text
            if _looks_useful(text):
                return text
        return best_text
    finally:
        doc.close()


def _extract_image_text(file_path: str) -> str:
    with Image.open(file_path) as image:
        return _ocr_image(image.convert("RGB"))


def extractText(file_path: str) -> str:
    """Return raw OCR text for a PDF or image file ('' when not OCR-able)."""
    detected = sniff(file_path)
    if detected == "pdf":
        return _extract_pdf_text(file_path)
    if detected in ("jpeg", "png") or detected == "image":
        return _extract_image_text(file_path)
    return ""

# --- Identity-field extraction ----------------------------------------------


def _extract_cnic(text: str) -> str | None:
    match = CNIC_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    return digits if len(digits) == 13 else None


_NAME_NOISE = {
    "name", "\u0646\u0627\u0645",
    "father", "husband", "spouse", "mother", "wife", "son", "daughter",
    "gender", "m", "f", "signature", "date", "issue", "expiry",
    "cnic", "nic", "identity", "card", "no", "national",
}


def _clean_name(raw: str) -> str | None:
    raw = raw.strip(" \t|:.,-_")
    # Cut at the next known label token (handles "MUHAMMAD ALI Gender: M").
    for token in _NAME_NOISE:
        raw = re.split(rf"\b{re.escape(token)}\b[\s:.-]*", raw, flags=re.IGNORECASE)[0]
    # Drop any inline CNIC digits.
    raw = re.sub(CNIC_RE.pattern, "", raw)
    raw = re.sub(r"\s{2,}", " ", raw).strip(" \t:.,-_")

    words = [w for w in raw.split() if re.fullmatch(r"[A-Za-z'\u06c1\u06be.\-]+", w)]
    if words and 1 <= len(words) <= 5:
        return " ".join(words)
    return None


def _extract_name(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        match = NAME_LABEL_RE.search(line)
        if not match:
            continue

        after = line[match.end():].strip(" \t|:.-_")
        candidate = after if after else None

        # Label had nothing after it on the same line: probe the next line.
        if not candidate:
            for j in range(index + 1, min(index + 3, len(lines))):
                if NAME_LABEL_RE.search(lines[j]):
                    break
                if OTHER_LABEL_RE.search(lines[j]):
                    break
                nxt = lines[j].strip(" \t|:.-_")
                if nxt and re.search(r"[A-Za-z]", nxt):
                    candidate = nxt
                    break

        if candidate:
            cleaned = _clean_name(candidate)
            if cleaned:
                return cleaned
    return None


def extractCnicAndName(text: str) -> tuple[str | None, str | None]:
    """Return (name, cnic). CNIC is normalized to 13 digits, name is whitespace-normalized."""
    cnic = _extract_cnic(text)
    name = _extract_name(text)
    if name:
        name = " ".join(name.split())
    return name, cnic