"""Generate real PDF fixtures for live end-to-end testing of the document service.

Real PDFs (not text files) are required: extractText() renders a PDF page to an
image and runs Tesseract OCR on it, so these fixtures exercise the genuine
extraction path rather than a shortcut.

Usage: venv\\Scripts\\python.exe make_test_pdfs.py <output_dir>
"""
import sys
from pathlib import Path

import pymupdf as fitz

CNIC_A = "42101-1234567-1"   # Asim Khan
CNIC_B = "35202-7654321-9"   # Bilal Ahmed

LETTER_A = [
    "EMPLOYMENT OFFER LETTER",
    "",
    "Employee Name: Asim Khan",
    f"CNIC: {CNIC_A}",
    "Designation: Software Engineer",
    "Date of Joining: 2024-01-01",
]

LETTER_B = [
    "EMPLOYMENT OFFER LETTER",
    "",
    "Employee Name: Asim Khan",
    f"CNIC: {CNIC_B}",
    "Designation: Software Engineer",
    "Date of Joining: 2024-01-01",
]

LETTER_NO_CNIC = [
    "EMPLOYMENT OFFER LETTER",
    "",
    "Employee Name: Asim Khan",
    "Designation: Software Engineer",
    "Date of Joining: 2024-01-01",
]

CNIC_CARD_A = [
    "NATIONAL IDENTITY CARD",
    "",
    "Name: Asim Khan",
    f"CNIC Number: {CNIC_A}",
    "Date of Birth: 05-08-1990",
]

CNIC_CARD_B = [
    "NATIONAL IDENTITY CARD",
    "",
    "Name: Asim Khan",
    f"CNIC Number: {CNIC_B}",
    "Date of Birth: 05-08-1990",
]

NDA = [
    "NON-DISCLOSURE AGREEMENT",
    "",
    "Name: Asim Khan",
    "I agree to keep all company information confidential.",
    "Designation: Software Engineer",
    "Date of Joining: 2024-01-01",
]

NDA_OTHER_PERSON = [
    "NON-DISCLOSURE AGREEMENT",
    "",
    "Name: Bilal Ahmed",
    "I agree to keep all company information confidential.",
    "Designation: Software Engineer",
    "Date of Joining: 2024-01-01",
]

# ── Replay fixtures (scenario A) ─────────────────────────────────────────────
# These must be byte-DISTINCT from the employee's reference document, otherwise
# the reference-match exact-hash fast path legitimately auto-approves the first
# submission and the "first submission must go to review" assertion stops being
# a test of anything. Same person, same CNIC, different designation -> different
# bytes.
LETTER_REF_A = [
    "EMPLOYMENT OFFER LETTER",
    "",
    "Employee Name: Asim Khan",
    f"CNIC: {CNIC_A}",
    "Designation: Senior Engineer",
    "Date of Joining: 2024-01-01",
]

LETTER_REPLAY_A = [
    "EMPLOYMENT OFFER LETTER",
    "",
    "Employee Name: Asim Khan",
    f"CNIC: {CNIC_A}",
    "Designation: Software Engineer",
    "Date of Joining: 2024-01-01",
]

LETTER_REPLAY_B = [
    "EMPLOYMENT OFFER LETTER",
    "",
    "Employee Name: Bilal Ahmed",
    f"CNIC: {CNIC_B}",
    "Designation: Software Engineer",
    "Date of Joining: 2024-01-01",
]


def write_pdf(path: Path, lines: list[str]) -> None:
    doc = fitz.open()
    page = doc.new_page()
    y = 80
    for line in lines:
        # A large, high-contrast font so Tesseract reads it reliably at 150 DPI.
        page.insert_text((70, y), line, fontsize=17, fontname="helv")
        y += 34
    doc.save(str(path))
    doc.close()
    print(f"  wrote {path.name} ({path.stat().st_size} bytes)")


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "_test_pdfs")
    out.mkdir(parents=True, exist_ok=True)
    print(f"writing fixtures to {out.resolve()}")
    for name, lines in {
        "letter_cnic_a.pdf": LETTER_A,
        "letter_cnic_b.pdf": LETTER_B,
        "letter_no_cnic.pdf": LETTER_NO_CNIC,
        "cnic_card_a.pdf": CNIC_CARD_A,
        "cnic_card_b.pdf": CNIC_CARD_B,
        "nda_a.pdf": NDA,
        "nda_b.pdf": NDA_OTHER_PERSON,
        "letter_ref_a.pdf": LETTER_REF_A,
        "letter_replay_a.pdf": LETTER_REPLAY_A,
        "letter_replay_b.pdf": LETTER_REPLAY_B,
    }.items():
        write_pdf(out / name, lines)
    print("done")


if __name__ == "__main__":
    main()
