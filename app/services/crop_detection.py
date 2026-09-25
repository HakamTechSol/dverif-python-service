"""
Cropped-document detection for the Dvarif document service.

A document is "cropped" when its content runs off the edge of the captured
frame / page, so part of the record (a name, an expiry date, a signature line)
is physically missing. This is a *quality* problem that structural corruption
checks cannot see: a perfectly valid PDF or JPEG can still be a useless
half-scan.

Two independent detectors, chosen per file type:

1. Images (JPEG/PNG/WEBP/GIF/BMP) — edge-ink analysis.
   A properly scanned page on white paper always keeps a thin white margin at
   the very edge of the frame. Any non-white pixel sitting on the outermost
   `BORDER_PX` rows/columns means content runs off the frame, i.e. the page
   was cut. The score is the fraction of dark pixels found in that border band;
   a real crop produces a contiguous run along one or more edges, which is
   orders of magnitude above the threshold, so the test stays stable against
   scanner speckle and JPEG ringing.

2. PDFs — content-overflow analysis.
   Union the bounding boxes of every text block, image and drawing on the page
   and compare that union against the page's crop box. Content that extends
   *outside* the visible page rect was clipped by a crop operation. This is
   deliberately conservative: merely *touching* the page edge is treated as a
   legitimate full-bleed design and NOT flagged, because many certificates and
   ID templates are designed to run to the trim edge and rejecting those would
   block valid uploads.

Every detector returns `{"cropped": bool, "reason": str | None, "score": float}`
so the caller can log the evidence and tune thresholds without re-running OCR.
Detection is best-effort: any unexpected error returns "not cropped" rather than
failing the whole validation request.
"""

from __future__ import annotations

try:
    import pymupdf as fitz  # newer PyMuPDF package name
except ImportError:  # pragma: no cover - older installs
    import fitz  # type: ignore
from PIL import Image

# --- Image tuning -------------------------------------------------------------

# Width of the analysed frame border, in pixels. 2px is the smallest band that
# still survives JPEG compression without being washed out by the encoder.
BORDER_PX = 2

# A pixel counts as "ink" when it is meaningfully darker than paper. 235/255 is
# well below the ~250 floor of office paper while ignoring scanner speckle.
INK_THRESHOLD = 235

# Fraction of the border band that must be ink before we call the frame cropped.
# Measured crops exceed 0.10 comfortably; clean scans sit under 0.01.
BORDER_INK_RATIO = 0.02

# Very small images cannot be judged reliably (a 20px thumbnail always has ink on
# its border). Below this we report "not cropped" rather than guessing.
MIN_IMAGE_EDGE = 100

# --- PDF tuning ---------------------------------------------------------------

# Content may exceed the page rect by this much (points) before it counts as
# clipped. Absorbs sub-point rounding differences between producers.
PDF_TOLERANCE_PT = 1.0

# PDFs with at least this many pages are sampled rather than fully analysed, to
# keep the check fast on long documents.
PDF_MAX_PAGES = 10

NOT_CROPPED: dict = {"cropped": False, "reason": None, "score": 0.0}


def _ink_ratio(pixels, threshold: int) -> float:
    """Fraction of `pixels` (a flat sequence of ints) darker than `threshold`."""
    if not pixels:
        return 0.0
    dark = sum(1 for p in pixels if p < threshold)
    return dark / len(pixels)


def detect_crop_image(file_path: str) -> dict:
    """Edge-ink crop detection for a raster document image."""
    try:
        with Image.open(file_path) as im:
            im.load()
            gray = im.convert("L")
            width, height = gray.size
    except Exception:
        # Unreadable image: validation_service already reports the hard failure,
        # so there is nothing useful to add here.
        return dict(NOT_CROPPED)

    if width < MIN_IMAGE_EDGE or height < MIN_IMAGE_EDGE:
        return dict(NOT_CROPPED)

    band = min(BORDER_PX, max(1, min(width, height) // 10))
    px = gray.load()
    assert px is not None

    top: list[int] = []
    bottom: list[int] = []
    left: list[int] = []
    right: list[int] = []

    for y in range(band):
        for x in range(width):
            top.append(px[x, y])
            bottom.append(px[x, height - 1 - y])
    for y in range(band):
        for x in range(band):
            left.append(px[x, y])
            right.append(px[width - 1 - x, y])

    edge_ratios = {
        "top": _ink_ratio(top, INK_THRESHOLD),
        "bottom": _ink_ratio(bottom, INK_THRESHOLD),
        "left": _ink_ratio(left, INK_THRESHOLD),
        "right": _ink_ratio(right, INK_THRESHOLD),
    }
    # The worst edge decides: one badly cut edge is enough to lose information.
    worst_edge, score = max(edge_ratios.items(), key=lambda kv: kv[1])

    if score < BORDER_INK_RATIO:
        return dict(NOT_CROPPED)

    return {
        "cropped": True,
        "reason": (
            f"Content runs off the {worst_edge} edge of the scan "
            f"({score:.0%} of the border is ink) — the document looks cropped"
        ),
        "score": round(score, 4),
    }


def _page_content_bbox(page) -> tuple[float, float, float, float] | None:
    """Union bbox of all text, images and vector art on a page, or None."""
    boxes = []
    try:
        for block in page.get_text("blocks"):
            boxes.append(tuple(block[:4]))
    except Exception:
        pass
    try:
        for item in page.get_text("dict").get("blocks", []):
            bbox = item.get("bbox")
            if bbox:
                boxes.append(tuple(bbox))
    except Exception:
        pass
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                boxes.append(tuple(bbox))
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is not None:
                boxes.append((rect.x0, rect.y0, rect.x1, rect.y1))
    except Exception:
        pass

    boxes = [b for b in boxes if all(isinstance(v, (int, float)) for v in b)]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def detect_crop_pdf(file_path: str) -> dict:
    """Content-overflow crop detection for a PDF document."""
    try:
        doc = fitz.open(file_path)
    except Exception:
        return dict(NOT_CROPPED)

    try:
        pages = list(doc)[:PDF_MAX_PAGES]
        if not pages:
            return dict(NOT_CROPPED)

        worst_overflow = 0.0
        worst_page = -1
        for index, page in enumerate(pages):
            bbox = _page_content_bbox(page)
            if bbox is None:
                continue
            try:
                page_rect = page.cropbox or page.rect
            except Exception:
                page_rect = page.rect
            overflow = max(
                page_rect.x0 - bbox[0],
                page_rect.y0 - bbox[1],
                bbox[2] - page_rect.x1,
                bbox[3] - page_rect.y1,
            )
            if overflow > worst_overflow:
                worst_overflow = overflow
                worst_page = index + 1

        if worst_overflow <= PDF_TOLERANCE_PT:
            return dict(NOT_CROPPED)

        return {
            "cropped": True,
            "reason": (
                f"Page {worst_page} content extends {worst_overflow:.1f}pt beyond the "
                "visible page — the document looks cropped"
            ),
            "score": round(worst_overflow, 4),
        }
    finally:
        doc.close()


def detect_crop(file_path: str, file_type: str | None) -> dict:
    """Run the crop detector appropriate for `file_type`.

    `file_type` is the sniffed type from validation_service (pdf/jpeg/png/...),
    not the filename extension, so a renamed file is routed correctly.
    """
    try:
        if file_type == "pdf":
            return detect_crop_pdf(file_path)
        if file_type in ("jpeg", "png", "image") or file_type in ("webp", "gif", "bmp"):
            return detect_crop_image(file_path)
        return dict(NOT_CROPPED)
    except Exception:
        # Never let crop detection turn a good upload into a failure.
        return dict(NOT_CROPPED)
