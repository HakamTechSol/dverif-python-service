"""
OCR text extraction + canonical identity-field extraction for Dvarif.

Pipeline:
  extractText(file)      PDF -> render to image (150 DPI, try page 1, fall
                         back to highest-text-density page) -> Tesseract OCR;
                         images are OCR'd directly.
  extractFields(t, type) schema-driven extraction (see
                         app.core.document_schemas): every schema field is
                         returned with a canonical snake_case name (name, cnic,
                         dob, designation, ...) and a per-field confidence.
                         Fields the document type declares but the text does
                         not show come back as {"value": None,
                         "confidence": "not_visible"} instead of being dropped.
"""

import os
import re
from datetime import datetime

try:
    import pymupdf as fitz  # newer PyMuPDF package name
except ImportError:  # pragma: no cover - older installs
    import fitz  # type: ignore
import pytesseract
from PIL import Image

from app.core.document_schemas import get_schema
from app.services.validation_service import sniff

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
_DATE_NUMERIC_RE = re.compile(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\s*")

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

# --- Label vocabulary --------------------------------------------------------


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
    raw = re.sub(r"\s{2,}", " ", raw).strip(" \t|:.,-_")

    words = [w for w in raw.split() if re.fullmatch(r"[A-Za-z'\u06c1\u06be.\-]+", w)]
    if words and 1 <= len(words) <= 5:
        return " ".join(words)
    return None


# Canonical field -> the label phrases the source document may use. The first
# label on a line wins; scanning is ordered by the list order here.
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "name": ("name", "full name", "\u0646\u0627\u0645"),
    "cnic": ("cnic", "national id", "national identity", "\u0634\u0646\u0627\u062e\u062a\u06cc"),
    "dob": ("date of birth", "birth date", "dob"),
    "passport_no": ("passport number", "passport no", "passport"),
    "designation": ("designation", "position", "post", "\u0639\u06c1\u062f\u06c1"),
    "joining_date": ("date of joining", "joining date", "date joined"),
    "duration": ("duration", "period", "tenure"),
    "degree": ("degree", "qualification"),
    "session": ("session", "batch"),
    "year": ("year"),
    "resignation_date": ("resignation date", "resignation", "last working day", "relieving date"),
    "effective_date": ("effective date", "effective from"),
    "increment": ("increment"),
    "bank_account": ("bank account", "account number", "iban"),
    "tax_year": ("tax year"),
    "recommendation_date": ("recommendation date", "recommendation"),
}


def _field_stop_tokens() -> frozenset[str]:
    """Any token that should terminate a field value (labels + name noise)."""
    tokens = set(_NAME_NOISE)
    for labels in _FIELD_LABELS.values():
        for label in labels:
            for part in label.split():
                if len(part) >= 2:
                    tokens.add(part)
    return frozenset(tokens)


_FIELD_STOP_TOKENS = _field_stop_tokens()


def _cut_value(raw: str) -> str | None:
    """Trim a value and cut it at the next known label / noise token."""
    raw = raw.strip(" \t|:.,-_")
    for token in _FIELD_STOP_TOKENS:
        raw = re.split(rf"\b{re.escape(token)}\b[\s:.-]*", raw, flags=re.IGNORECASE)[0]
    raw = re.sub(r"\s{2,}", " ", raw).strip(" \t|:.,-_")
    return raw or None


def _extract_labeled_field(
    text: str,
    labels: tuple[str, ...],
) -> tuple[str, str] | None:
    """(value, confidence) for the first matching label.

    Value on the SAME line right after the label -> "high"; value probed from
    the immediately following line -> "low" (weaker adjacency signal).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        for label in labels:
            match = re.search(rf"\b{re.escape(label)}\b", line, re.IGNORECASE)
            if not match:
                continue

            after = line[match.end():].strip(" \t|:.,-_")
            if after:
                value = _cut_value(after)
                if value:
                    return value, "high"

            if index + 1 < len(lines):
                nxt = lines[index + 1].strip(" \t|:.,-_")
                if nxt and not _looks_like_label_line(nxt):
                    value = _cut_value(nxt)
                    if value:
                        return value, "low"
    return None


def _looks_like_label_line(line: str) -> bool:
    """A line that is itself a label (e.g. 'Name:' with nothing after it)."""
    for labels in _FIELD_LABELS.values():
        for label in labels:
            if re.search(rf"\b{re.escape(label)}\b[\s:.-]*$", line, re.IGNORECASE):
                return True
    return bool(OTHER_LABEL_RE.search(line.lstrip(" \t|:.")))


def _normalize_date(raw: str) -> str | None:
    """Best-effort date -> ISO YYYY-MM-DD (None when unparseable)."""
    match = _DATE_NUMERIC_RE.fullmatch(raw)
    if match:
        a, b, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if y < 100:
            y += 2000
        if b > 12 and a <= 12:  # "30/01/2024" style month-first
            day, month = b, a
        else:
            day, month = a, b
        try:
            return datetime(y, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None
    iso_match = re.fullmatch(r"\s*(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})\s*", raw)
    if iso_match:
        try:
            return datetime(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            ).strftime("%Y-%m-%d")
        except ValueError:
            return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_date_field(text: str, labels: tuple[str, ...]) -> tuple[str, str] | None:
    """Labeled date field; the value is normalized to ISO when parseable."""
    found = _extract_labeled_field(text, labels)
    if not found:
        return None
    value, confidence = found
    normalized = _normalize_date(value)
    return (normalized or value), confidence


def _extract_name_field(text: str) -> tuple[str, str] | None:
    """(name, confidence) from a Name/نام label. Same-line -> high, probe -> low."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        match = NAME_LABEL_RE.search(line)
        if not match:
            continue

        after = line[match.end():].strip(" \t|:.-_")
        candidate = after if after else None
        same_line = bool(candidate)

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
                return cleaned, ("high" if same_line else "low")
    return None


def _extract_field(field: str, text: str) -> tuple[str, str] | None:
    """Canonical field extraction -> (value, confidence) or None when not found."""
    if field == "name":
        return _extract_name_field(text)
    if field == "cnic":
        value = _extract_cnic(text)
        return (value, "high") if value else None
    if field == "dob":
        return _extract_date_field(text, _FIELD_LABELS["dob"])
    if field in ("joining_date", "resignation_date", "effective_date", "recommendation_date"):
        return _extract_date_field(text, _FIELD_LABELS[field])
    if field in _FIELD_LABELS:
        return _extract_labeled_field(text, _FIELD_LABELS[field])
    return None


def extractFields(text: str, document_type: str | None = None) -> dict:
    """Extract canonical identity fields per the resolved document-type schema.

    Returns {"document_type": <canonical key>, "fields": {field: ExtractedField}}.
    Every field declared by the schema is always present; fields that could not
    be extracted get {"value": None, "confidence": "not_visible"}.
    """
    key, schema = get_schema(document_type)
    fields: dict[str, dict] = {}
    for field in schema:
        found = _extract_field(field, text)
        if found:
            value, confidence = found
            fields[field] = {"value": value, "confidence": confidence}
        else:
            fields[field] = {"value": None, "confidence": "not_visible"}
    return {"document_type": key, "fields": fields}


def extractCnicAndName(text: str) -> tuple[str | None, str | None]:
    """(name, cnic) convenience wrapper — superseded by extractFields()."""
    name = _extract_name_field(text)
    if name:
        return " ".join(name[0].split()), _extract_cnic(text)
    return None, _extract_cnic(text)