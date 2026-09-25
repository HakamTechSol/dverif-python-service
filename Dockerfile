# syntax=docker/dockerfile:1
#
# Dvarif Document Service — multi-stage image.
#
# The service only needs: Python + the runtime packages from requirements.txt
# + a system Tesseract. requirements.txt also lists pytest/httpx for LOCAL
# development, so stage 1 builds wheels from it and stage 2 installs ONLY the
# runtime subset from those wheels — the dev/test dependencies never land in
# the production image.

# ---- Stage 1: build wheels for the runtime packages -------------------------
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir --wheel-dir=/build/wheels -r requirements.txt

# ---- Stage 2: runtime --------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=5001 \
    MAX_UPLOAD_SIZE_MB=15 \
    TESSERACT_LANG=eng

# System dependency for OCR: the Tesseract binary + English traineddata.
# PyMuPDF, Pillow and rapidfuzz ship self-contained manylinux wheels, so no
# extra image libraries are needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# The service writes only per-request temp files; run as a non-root user.
RUN useradd --create-home --uid 5000 appuser

WORKDIR /srv/docservice

# Runtime-only packages, installed from the wheels built in stage 1.
COPY --from=builder /build/wheels /build/wheels
RUN pip install --no-cache-dir --no-index --find-links=/build/wheels \
        fastapi \
        "uvicorn[standard]" \
        pytesseract \
        Pillow \
        PyMuPDF \
        rapidfuzz \
        python-multipart \
        pydantic \
        pydantic-settings \
    && rm -rf /build/wheels

COPY app ./app

USER appuser

EXPOSE 5001

# NOTE: fail-closed. The API key is NOT baked in; DOC_SERVICE_API_KEY must be
# provided via `docker run -e DOC_SERVICE_API_KEY=...`. While it is missing,
# every endpoint except /health returns 503.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001"]