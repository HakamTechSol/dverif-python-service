"""
Canonical per-document-type field schemas for the Dvarif document service.

Every document_type known to the product maps to a canonical schema that
declares which identity fields it is expected to carry. Fields use a single
snake_case vocabulary (name, cnic, dob, designation, ...) no matter how the
source document labels them, so the OCR service and the matcher share one
canonical representation.

Semantics:
  True  -> the field is REQUIRED by this document type. A required field that
           is not visible, or a required field that differs between two
           documents, fails a /match comparison.
  False -> the field is OPTIONAL. An optional field absent on one side of a
           comparison is simply skipped without failing the whole match.

SYNC: DOCUMENT_TYPE_ALIASES mirrors the document-type catalog in
      dvarif-verified/src/lib/documentTypes.ts (EMPLOYEE_DOCUMENT_TYPES).
      Every catalog label resolves here; free-text document types that match
      no label fall back to GENERIC_SCHEMA.
"""

import re

# ---------------------------------------------------------------------------
# Canonical schemas (snake_case field name -> required?/optional?)
# ---------------------------------------------------------------------------

DOCUMENT_TYPE_SCHEMAS: dict[str, dict[str, bool]] = {
    # Government identity documents.
    "cnic": {"name": True, "cnic": True, "dob": True},
    "passport": {"name": True, "passport_no": False, "dob": False},

    # Hiring / employment letters.
    "offer_letter": {"name": True, "designation": False, "joining_date": False},
    "appointment_letter": {"name": True, "designation": False, "joining_date": False},
    "employment_contract": {"name": True, "designation": False, "joining_date": False},
    "experience_letter": {"name": True, "designation": False, "duration": False},
    "reference_letter": {"name": True, "recommendation_date": False},

    # Career / education records.
    "resume": {"name": True, "designation": False, "cnic": False},
    "application_form": {"name": True, "cnic": False, "dob": False},
    "education_certificate": {"name": True, "degree": False, "session": False},
    "transcript": {"name": True, "degree": False, "session": False},

    # Movement / change letters.
    "relieving_letter": {"name": True, "designation": False, "resignation_date": False},
    "resignation_letter": {"name": True, "designation": False, "resignation_date": False},
    "promotion_letter": {"name": True, "designation": False, "effective_date": False},
    "increment_letter": {"name": True, "designation": False, "increment": False},
    "transfer_letter": {"name": True, "effective_date": False},

    # Personal / financial / compliance documents.
    "bank_details": {"name": True, "bank_account": False},
    "tax_document": {"name": True, "tax_year": False},
    "background_check": {"name": True},
    "medical_certificate": {"name": True},
    "character_certificate": {"name": True},
    "emergency_form": {"name": True},
    "leave_record": {"name": True},
    "attendance_record": {"name": True},
    "performance_review": {"name": True},
    "training_record": {"name": True},
    "disciplinary": {"name": True},
    "exit_form": {"name": True},
    "clearance_form": {"name": True},
    "settlement": {"name": True},

    # Identity-poor document types: best-effort only, nothing required.
    "photo": {"name": False},
    "employee_id": {"name": False},
    "policy_ack": {"name": False},
    "legal_agreement": {"name": False},
    "onboarding": {"name": False},
    "asset_handover": {"name": False},
    "job_description": {"designation": True, "name": False},
    "closing_checklist": {"name": False},
}

# Fallback for free-text document types that resolve to no known schema.
# Keeps the classic name + cnic behavior for anything unrecognized.
GENERIC_SCHEMA: dict[str, bool] = {"name": True, "cnic": False}


def _normalize_key(value: str) -> str:
    """Lowercase alphanumerics-only key: 'CNIC / National ID Copy' -> 'cnic national id copy'."""
    return re.sub(r"[^0-9a-z]+", " ", value.lower()).strip()


# Catalog labels -> nearest canonical schema key. Mirrors EMPLOYEE_DOCUMENT_TYPES
# in dvarif-verified/src/lib/documentTypes.ts (47 entries). Keys are
# _normalize_key() output so lookups are case/punctuation-insensitive.
DOCUMENT_TYPE_ALIASES: dict[str, str] = {
    "employee application form": "application_form",
    "cv resume": "resume",
    "recent photograph": "photo",
    "cnic national id copy": "cnic",
    "passport copy if applicable": "passport",
    "educational certificates": "education_certificate",
    "educational transcripts mark sheets": "transcript",
    "experience certificates": "experience_letter",
    "previous employment relieving letter": "relieving_letter",
    "reference recommendation letters": "reference_letter",
    "employee information form": "application_form",
    "employment appointment letter": "appointment_letter",
    "job description": "job_description",
    "offer letter": "offer_letter",
    "employment contract agreement": "employment_contract",
    "nda non disclosure agreement": "legal_agreement",
    "company policies acknowledgment": "policy_ack",
    "code of conduct agreement": "legal_agreement",
    "it computer usage policy acknowledgment": "policy_ack",
    "data privacy confidentiality agreement": "legal_agreement",
    "bank account salary details": "bank_details",
    "tax information tax documents": "tax_document",
    "emergency contact form": "emergency_form",
    "medical fitness certificate if required": "medical_certificate",
    "background verification report if applicable": "background_check",
    "police character certificate if required": "character_certificate",
    "joining onboarding checklist": "onboarding",
    "employee id card record": "employee_id",
    "asset handover form": "asset_handover",
    "laptop computer handover form": "asset_handover",
    "sim mobile other equipment handover": "asset_handover",
    "leave records": "leave_record",
    "attendance records": "attendance_record",
    "performance evaluation records": "performance_review",
    "training certification records": "training_record",
    "warning disciplinary records if applicable": "disciplinary",
    "promotion salary revision letters": "promotion_letter",
    "transfer department change records": "transfer_letter",
    "increment letter": "increment_letter",
    "resignation letter": "resignation_letter",
    "exit interview form": "exit_form",
    "clearance form": "clearance_form",
    "final settlement record": "settlement",
    "experience service certificate": "experience_letter",
    "relieving letter": "relieving_letter",
    "company asset return form": "asset_handover",
    "employee file closing checklist": "closing_checklist",
    # Common free-text short forms used by submissions / tests.
    "passport": "passport",
    "national id card": "cnic",
    "degree": "education_certificate",
    "employment letter": "appointment_letter",
}


def resolve_document_type(document_type: str | None) -> str:
    """Map an incoming document_type string to a canonical schema key (lowercase, no spaces).
    Unknown types resolve to 'generic'."""
    if not document_type:
        return "generic"
    key = _normalize_key(str(document_type))
    if key in DOCUMENT_TYPE_SCHEMAS:
        return key
    return DOCUMENT_TYPE_ALIASES.get(key, "generic")


def get_schema(document_type: str | None) -> tuple[str, dict[str, bool]]:
    """Return (canonical_key, schema_dict) for a document_type string."""
    key = resolve_document_type(document_type)
    return key, DOCUMENT_TYPE_SCHEMAS.get(key, GENERIC_SCHEMA)