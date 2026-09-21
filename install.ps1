# Dvarif Document Service — Windows setup script.
#
# Creates a virtualenv INSIDE this python-backend folder, installs the
# requirements, and prints Tesseract OCR install instructions.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $here

Write-Host "=== Dvarif Document Service setup ===" -ForegroundColor Cyan

# 1. Locate a Python interpreter (3.10+ recommended)
$py = $null
foreach ($candidate in @("python", "py -3", "python3")) {
    try {
        & $candidate --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $py = $candidate; break }
    } catch { }
}
if (-not $py) {
    Write-Host "Python not found. Install Python 3.10+ from https://www.python.org/downloads/ and re-run." -ForegroundColor Red
    exit 1
}
Write-Host "Using Python: $py" -ForegroundColor Green

# 2. Create virtualenv inside this folder (idempotent)
if (-not (Test-Path -LiteralPath ".\venv\Scripts\Activate.ps1")) {
    Write-Host "Creating virtualenv (venv) in $here ..."
    & $py -m venv .\venv
    if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create virtualenv." -ForegroundColor Red; exit 1 }
} else {
    Write-Host "Virtualenv already exists." -ForegroundColor Yellow
}

# 3. Install requirements
$pip = ".\venv\Scripts\python.exe"
Write-Host "Installing requirements.txt ..."
& $pip -m pip install --upgrade pip
& $pip -m pip install -r .\requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed." -ForegroundColor Red; exit 1 }

Write-Host "`n=== Dependencies installed inside .\venv ===" -ForegroundColor Green

# 4. Tesseract instructions (tesseract.exe is NOT a pip package)
Write-Host @"

---------------------------------------------------------------------------
TESSERACT OCR (needed for the OCR/matching milestones, not for this scaffold)
---------------------------------------------------------------------------
1. Download the UB Mannheim installer:
       https://github.com/UB-Mannheim/tesseract/wiki
   (pick the latest 64-bit installer, e.g. tesseract-ocr-w64-setup-*.exe)
2. Run the installer. The `eng` language data ships with it.
3. Add the install folder to PATH (system-wide):
       C:\Program Files\Tesseract-OCR
   PowerShell (admin), then reopen the terminal:
       [Environment]::SetEnvironmentVariable('Path',
         [Environment]::GetEnvironmentVariable('Path','Machine') + ';C:\Program Files\Tesseract-OCR','Machine')
4. Verify in a NEW terminal:
       tesseract --version
---------------------------------------------------------------------------
"@ -ForegroundColor Yellow

Write-Host "`nStart the service with:" -ForegroundColor Cyan
Write-Host "    .\venv\Scripts\activate" -ForegroundColor White
Write-Host "    uvicorn app:app --port 5001" -ForegroundColor White
Write-Host "`nSmoke test:  Invoke-RestMethod http://localhost:5001/health" -ForegroundColor White

Write-Host "`nSetup complete." -ForegroundColor Green