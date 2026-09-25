# Dvarif Document Service (python-backend)

Standalone FastAPI microservice for the Dvarif verification platform.
It runs as its **own process** (`uvicorn app.main:app --port 5001`) — fully
separate from the Node.js backend (`backend/`, port 5000) and the React
frontend (`dvarif-verified/`, port 8080). Codebases share nothing: no
`node_modules`, no imports, no shared folder.

The Node.js backend calls this service over HTTP (`localhost:5001`) using
an API key. This folder is a sibling of `backend/`, not nested inside it.

## Current scope

- `GET  /health` — liveness probe (no auth), returns `{"status": "ok"}`
- `POST /validate` — **real corrupt-file detection** on an uploaded file.
  Two layers of defence:
  1. **Magic-byte sniffing** — the content must match the declared type
     (PDF `%PDF-`, JPEG `FF D8 FF`, PNG `\x89PNG`, ZIP `PK` for
     DOCX/XLSX, OLE2 for old `.doc`/`.xls`, or plain text). A renamed
     file is rejected with `File type mismatch`.
  2. **Structural parse** — the file is actually opened:
     - PDF — PyMuPDF open + page count + trailing `%%EOF` marker
     - JPEG/PNG/images — Pillow `verify()` + full decode (`load()`)
     - ZIP/DOCX/XLSX — `zipfile.testzip()` CRC check + required Office
       package parts
     - text/CSV — checked by content, no structural parse

  Returns `{valid: bool, reason: string|null, file_type: string|null}`.
- `POST /ocr/extract` — OCR the document (PDF is rendered to an image at
  150 DPI; page 1 first, then the highest-text-density page) and extract
  canonical identity fields via Tesseract against the document type's field
  schema. Accepts an optional multipart `document_type` field to pick the
  schema (unknown/omitted -> generic `name` + `cnic`). Returns
  `{document_type: string, fields: {name|dob|designation|...: {value,
  confidence}}}` where `confidence` is `high` / `low` / `not_visible` and a
  required-but-unfound field is reported as `not_visible` (not dropped).
- `POST /match` — compare two documents (multipart `file_a`, `file_b`, plus
  optional `document_type_a` / `document_type_b`). Only canonical fields
  common to both schemas are compared: exact fields (`cnic`, dates, ...) match
  only when equal, text fields (`name`, `designation`, ...) use rapidfuzz
  (≥88% = match). A required field that is `not_visible`, or that differs, fails
  the match; optional fields absent on one side are skipped. Confidence =
  average of the comparable field scores (0-100 each); `match=true` when
  confidence ≥ 75. Returns `{match: bool, confidence: number, reasons: string[]}`.
- `GET  /metrics` — Prometheus-style counters (API-key protected): per-endpoint
  request count / failure rate / latency, plus per-document-type outcome
  tallies (`document_service_outcome_total{endpoint, document_type, outcome}`)
  that feed later auto-approve vs manual-fallback analytics.

Uploads are capped at `MAX_UPLOAD_SIZE_MB` (default **15**, slightly above
the Node backend's 10MB multer limit). Requests advertising a larger
Content-Length are rejected with `413` before the body is read, and the
streaming write itself enforces the same per-file cap so a spoofed or
missing Content-Length cannot slip a bigger file through.

### `/ocr/extract` response shape (Step 8)

Given an optional multipart `document_type`, extraction is driven by the
canonical schema for that type (`app/core/document_schemas.py`, a mirror of
`EMPLOYEE_DOCUMENT_TYPES` in `dvarif-verified/src/lib/documentTypes.ts`). The
response no longer carries a flat `{name, cnic}` — every schema field always
appears under its canonical snake_case name:

```json
// POST /ocr/extract — multipart file + document_type: "CNIC / National ID Copy"
{
  "success": true,
  "data": {
    "document_type": "cnic",
    "fields": {
      "name": { "value": "Asim Khan",     "confidence": "high" },
      "cnic": { "value": "4210112345671", "confidence": "high" },
      "dob":  { "value": "1990-08-05",    "confidence": "high" }
    }
  }
}
```

Field-value semantics:

- `confidence` is `high` (value found on the same line as its label),
  `low` (value probed from the immediately following line) or `not_visible`
  (field declared by the schema but not found).
- A required-but-unfound field comes back as `{"value": null,
  "confidence": "not_visible"}` — it is **reported, never dropped**.
- Unknown or omitted `document_type` falls back to the `generic` schema
  (`name` required, `cnic` optional).

`app/services/match_service.py` compares ONLY canonical fields, so `/match`
and the Node backend's person-document cross-check share this one vocabulary:
exact fields (`cnic`, dates, account numbers) match only when equal; text
fields (`name`, `designation`, `degree`) use rapidfuzz ≥88; optional fields
absent on one side are skipped; a required field that is `not_visible` or
differs fails the comparison hard. Confidence = average of the comparable
field scores (0-100 each); `match=true` when confidence ≥ 75.

---

## Setup (Windows)

### 1. Create the virtualenv INSIDE this folder

From a terminal at `E:\Office Projects\Dvarif\python-backend`:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Or run the one-shot installer (does the same thing + installs deps +
prints Tesseract instructions):

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Install Tesseract OCR (required for the OCR endpoints)

Download the UB Mannheim installer, run it, and add
`C:\Program Files\Tesseract-OCR` to your `PATH`:

- https://github.com/UB-Mannheim/tesseract/wiki

Default install options are fine — the `eng` language data comes with the
installer. Verify with:

```powershell
tesseract --version
```

### 4. Configure the environment

`.env` already exists locally (gitignored) with a generated
`DOC_SERVICE_API_KEY`. The same key must be configured in the Node.js
backend's `.env` as `DOC_SERVICE_API_KEY` later. To regenerate:
copy `.env.example` to `.env` and set a long random string.

### 5. Run

```powershell
uvicorn app.main:app --port 5001
```

Binds `127.0.0.1` by default. To accept connections from other machines
for a real deployment (e.g. behind a reverse proxy), opt in explicitly —

```powershell
$env:HOST = "0.0.0.0"   # or put HOST=0.0.0.0 in .env
uvicorn app.main:app --port 5001
```

Or with auto-reload during development:

```powershell
uvicorn app.main:app --port 5001 --reload
```

### 6. Smoke-test

```powershell
Invoke-RestMethod http://localhost:5001/health
# -> { status = ok }

# With the API key (every endpoint except /health requires it):
$headers = @{ "X-API-Key" = $env:DOC_SERVICE_API_KEY }   # or paste the key
Invoke-RestMethod http://localhost:5001/validate -Method Post -Headers $headers -Form @{ file = Get-Item ".\requirements.txt" }
# -> success = True, data.valid = True (text file)
```

Without a correct `X-API-Key`, `/validate` returns `401`. A truncated
PDF/JPEG/PNG/ZIP returns `data.valid = False` with a reason.

---

## Project layout

```
python-backend/
├── app/
│   ├── main.py                  # FastAPI app: middleware stack + router mounting
│   ├── core/
│   │   ├── config.py            # pydantic-settings (DOC_SERVICE_API_KEY, HOST, PORT, MAX_UPLOAD_SIZE_MB)
│   │   ├── document_schemas.py  # canonical per-document-type field schemas (synced with the frontend catalog)
│   │   ├── security.py          # API-key middleware (X-API-Key check)
│   │   ├── logging.py           # structured JSON logger (stdout; no document content)
│   │   ├── metrics.py           # in-memory Prometheus-style counters for /metrics
│   │   └── observability.py     # outermost middleware: request id + latency + JSON log + tallies
│   ├── api/routes/              # route handlers only: health, validate, ocr, match, metrics
│   ├── services/                # business logic: validation, ocr, match
│   ├── schemas/                 # pydantic response models
│   └── utils/file_handling.py   # temp-file persistence for uploads
├── tests/                       # pytest + FastAPI TestClient suites (validate/ocr/match/security/metrics)
├── requirements.txt             # Python dependencies (+ pytest, httpx for dev)
├── Dockerfile                   # multi-stage container image (Python + runtime deps + Tesseract)
├── .dockerignore
├── install.ps1                  # Windows one-shot setup (venv + pip + Tesseract guide)
├── README.md                    # this file
├── .env.example                 # env template (no secrets)
├── .env                         # local secrets (gitignored)
└── .gitignore                   # excludes .env, venv/, __pycache__/
```

## API contract

| Endpoint | Auth | Input | Output |
|---|---|---|---|
| `GET /health` | none | — | `{"status": "ok"}` |
| `POST /validate` | API key | multipart `file` | `{valid, reason, file_type}` |
| `POST /ocr/extract` | API key | multipart `file` (+ optional `document_type`) | `{document_type, fields}` |
| `POST /match` | API key | multipart `file_a`, `file_b` (+ optional `document_type_a`, `document_type_b`) | `{match, confidence, reasons}` |
| `GET /metrics` | API key | — | Prometheus text (request/latency/failure + per-doc-type outcome counters) |

Every response body is wrapped as `{success, data}`; the API key is sent
via the `X-API-Key` header. Any wrong/missing key (except `/health`)
returns `401`.

---

## Logging & metrics

- **Structured logging** — each request produces exactly one JSON object on
  stdout (`ts`, `level`, `event`, `request_id`, `method`, `path`, `status`,
  `duration_ms`, `document_type`, `outcome`). `request_id` is echoed to the
  `X-Request-Id` response header. **Privacy contract:** extracted `name` /
  `cnic` values and raw OCR text are never logged — only document-type keys
  and outcome labels. This is enforced by design (the observability middleware
  receives only request metadata) and guarded by a regression test
  (`tests/test_security.py::test_logs_never_contain_extracted_pii`).
- **`GET /metrics`** — Prometheus text exposition, API-key protected:
  - `document_service_requests_total` / `document_service_failures_total` /
    `document_service_request_duration_seconds{count,sum}` — per endpoint.
  - `document_service_outcome_total{endpoint, document_type, outcome}` — e.g.
    `{endpoint="/match",document_type="cnic",outcome="match"}` for approval vs
    fallback analytics by document type.
  The registry is in-memory (single uvicorn worker); scrape it with the same
  `X-API-Key` the Node.js backend uses.

## Container deployment (Docker)

The included `Dockerfile` is multi-stage and installs only what the service
needs at runtime: Python 3.11 + the runtime packages from `requirements.txt`
(built as wheels in the builder stage so the dev/test deps never land in the
image) + system Tesseract (`tesseract-ocr` + `tesseract-ocr-eng`). It runs as
a non-root user and binds `0.0.0.0:5001`.

```powershell
docker build -t dvarif-docservice .
docker run --rm -p 5001:5001 `
  -e DOC_SERVICE_API_KEY="<same key as the Node backend>" `
  dvarif-docservice
```

The API key is **fail-closed**: not passed via `-e`, every endpoint except
`/health` returns `503`. To switch OCR language data, pass
`-e TESSERACT_LANG=...` (only `eng` data is bundled).

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest tests
```

Covers the API through FastAPI's TestClient (httpx): `/validate` (valid /
corrupt / renamed / oversized files), `/ocr/extract` and `/match` across two
document-type schemas (OCR text injected, no Tesseract needed), auth security
surface, and `/metrics` counters.