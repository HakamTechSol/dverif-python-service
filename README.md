# Dvarif Document Service (python-backend)

Standalone FastAPI microservice for the Dvarif verification platform.
It runs as its **own process** (`uvicorn app:app --port 5001`) — fully
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
  the identity fields via Tesseract. Returns
  `{name: string|null, cnic: string|null}` (CNIC normalized to 13 digits).
- `POST /match` — compare two documents (multipart `file_a`, `file_b`).
  Name is fuzzy-matched with rapidfuzz (≥88% ratio = name match), CNIC is
  compared exactly after normalization. Confidence = `0.6*name + 0.4*cnic`
  (0-100); `match=true` when confidence ≥ 75. Returns
  `{match: bool, confidence: number, reasons: string[]}`.

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

### 3. Install Tesseract OCR (required only for later OCR milestones)

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
uvicorn app:app --port 5001
```

Or with auto-reload during development:

```powershell
uvicorn app:app --port 5001 --reload
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
├── app.py              # FastAPI app: API-key middleware + all endpoints
├── validate.py         # corrupt-file detection (magic bytes + structural parse)
├── ocr.py              # PDF->image + Tesseract OCR + name/CNIC extraction
├── match.py            # two-document comparison (rapidfuzz + CNIC, confidence)
├── requirements.txt    # Python dependencies
├── install.ps1         # Windows one-shot setup (venv + pip + Tesseract guide)
├── README.md           # this file
├── .env.example        # env template (no secrets)
├── .env                # local secrets (gitignored)
└── .gitignore          # excludes .env, venv/, __pycache__/
```

## API contract

| Endpoint | Auth | Input | Output |
|---|---|---|---|
| `GET /health` | none | — | `{"status": "ok"}` |
| `POST /validate` | API key | multipart `file` | `{valid, reason, file_type}` |
| `POST /ocr/extract` | API key | multipart `file` | `{name, cnic}` |
| `POST /match` | API key | multipart `file_a`, `file_b` | `{match, confidence, reasons}` |

Every response body is wrapped as `{success, data}`; the API key is sent
via the `X-API-Key` header. Any wrong/missing key (except `/health`)
returns `401`.