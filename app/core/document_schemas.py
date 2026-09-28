"""
Canonical per-document-type field schemas for the Dvarif document service.

Every document_type known to the product maps to a canonical schema that
declares which identity fields it is expected to carry. Fields use a single
snake_case vocabulary (name, cnic, dob, designation, ...) no matter how the
source document labels them, so the OCR service and the matcher share one
canonical representation.

Semantics of the per-field boolean:
  True  -> the field is REQUIRED by this document type. A required field that
           is not visible, or a required field that differs between two
           documents, fails a /match comparison.
  False -> the field is OPTIONAL. An optional field absent on one side of a
           comparison is simply skipped without failing the whole match —
           EXCEPT for the fields in ALWAYS_COMPARE_FIELDS, which are never
           silently dropped (see below).

ALWAYS-COMPARED IDENTITY FIELDS
  extractFields() only ever emits the fields a schema declares, so a field left
  out of a schema is not merely optional — it is invisible to the matcher
  entirely. That is how a CNIC printed on a document could be read by OCR and
  then discarded before any comparison ran: 35 of the 39 schemas below did not
  declare `cnic` at all, and for those document types a /match could return
  confidence 100.0 on a NAME alone while the two documents carried visibly
  different identities. `cnic` is therefore declared on every schema, and
  ALWAYS_COMPARE_FIELDS marks it as one that may never be skipped: if either
  side carries a CNIC and the other does not, or the two differ, the comparison
  records an identity_mismatch instead of quietly reporting a clean match.

AUTO-MATCH-INELIGIBLE TYPES
  Some document types carry no identity at all — a photograph, an NDA, a policy
  acknowledgment, a handover form. Two of those can be textually near-identical
  while describing different people, and matching them proves nothing. They are
  listed in AUTO_MATCH_INELIGIBLE_TYPES and are never eligible for automatic
  approval; /match reports them with auto_match_eligible=False and a zero
  confidence so they always land with a human.

SYNC: DOCUMENT_TYPE_ALIASES mirrors the document-type catalog in
      dvarif-verified/src/lib/documentTypes.ts (EMPLOYEE_DOCUMENT_TYPES).
      Every catalog label resolves here; free-text document types that match
      no label fall back to GENERIC_SCHEMA.
"""

import re
import threading

# ---------------------------------------------------------------------------
# Canonical schemas (snake_case field name -> required?/optional?)
# ---------------------------------------------------------------------------

DOCUMENT_TYPE_SCHEMAS: dict[str, dict[str, bool]] = {
    # Government identity documents. `cnic` is the only type where the CNIC is
    # itself the document, so it is the only schema that can require it.
    "cnic": {"name": True, "cnic": True, "dob": True},
    "passport": {"name": True, "cnic": False, "passport_no": False, "dob": False},

    # Hiring / employment letters. A CNIC is frequently printed on these, so it
    # is declared (and therefore always-compared when present) but never
    # required — plenty of legitimate letters do not carry one.
    "offer_letter": {"name": True, "cnic": False, "designation": False, "joining_date": False},
    "appointment_letter": {"name": True, "cnic": False, "designation": False, "joining_date": False},
    "employment_contract": {"name": True, "cnic": False, "designation": False, "joining_date": False},
    "experience_letter": {"name": True, "cnic": False, "designation": False, "duration": False},
    "reference_letter": {"name": True, "cnic": False, "recommendation_date": False},

    # Career / education records.
    "resume": {"name": True, "cnic": False, "designation": False},
    "application_form": {"name": True, "cnic": False, "dob": False},
    "education_certificate": {"name": True, "cnic": False, "degree": False, "session": False},
    "transcript": {"name": True, "cnic": False, "degree": False, "session": False},

    # Movement / change letters.
    "relieving_letter": {"name": True, "cnic": False, "designation": False, "resignation_date": False},
    "resignation_letter": {"name": True, "cnic": False, "designation": False, "resignation_date": False},
    "promotion_letter": {"name": True, "cnic": False, "designation": False, "effective_date": False},
    "increment_letter": {"name": True, "cnic": False, "designation": False, "increment": False},
    "transfer_letter": {"cnic": False, "effective_date": True},

    # Personal / financial / compliance documents.
    "bank_details": {"name": True, "cnic": False, "bank_account": False},
    "tax_document": {"name": True, "cnic": False, "tax_year": False},
    "background_check": {"name": True, "cnic": False},
    "medical_certificate": {"name": True, "cnic": False},
    "character_certificate": {"name": True, "cnic": False},
    "emergency_form": {"name": True, "cnic": False},
    "leave_record": {"name": True, "cnic": False},
    "attendance_record": {"name": True, "cnic": False},
    "performance_review": {"name": True, "cnic": False},
    "training_record": {"name": True, "cnic": False},
    "disciplinary": {"name": True, "cnic": False},
    "exit_form": {"name": True, "cnic": False},
    "clearance_form": {"name": True, "cnic": False},
    "settlement": {"name": True, "cnic": False},

    # Identity-poor document types. These carry no identifying content of their
    # own — see AUTO_MATCH_INELIGIBLE_TYPES. `cnic` is still declared so the
    # extractor picks one up when a form does happen to print it (a bank-details
    # handover form, say), but nothing here is ever auto-approved.
    "photo": {"name": False, "cnic": False},
    "employee_id": {"name": False, "cnic": False},
    "policy_ack": {"name": False, "cnic": False},
    "legal_agreement": {"name": False, "cnic": False},
    "onboarding": {"name": False, "cnic": False},
    "asset_handover": {"name": False, "cnic": False},
    "job_description": {"cnic": False, "designation": True, "name": False},
    "closing_checklist": {"name": False, "cnic": False},
}

# Fallback for free-text document types that resolve to no known schema.
# Keeps the classic name + cnic behavior for anything unrecognized. `cnic` is
# always-compared here too, so an unrecognized type still cannot reach 100 on a
# name alone whenever either side shows a CNIC.
GENERIC_SCHEMA: dict[str, bool] = {"name": True, "cnic": False}

# Fields that decide WHO a document belongs to, and may never be skipped just
# because one side did not happen to show them. Ordinary optional fields absent
# on one side are skipped (a letter with no printed joining date is still the
# same letter); an identity field present on one side only is not the same
# situation at all — it means the two documents cannot be shown to describe the
# same person, which must be reported rather than averaged away.
ALWAYS_COMPARE_FIELDS: frozenset[str] = frozenset({"cnic"})

# Document types that can never produce an automatic approval.
#
# These describe an EVENT or a POLICY, not a person: two NDAs, two policy
# acknowledgments or two handover forms can be textually identical while
# belonging to different employees, so a high confidence across them is
# meaningless. The `name` field is optional on every one of them, which means
# that before this set existed a pair of NDAs sharing a name scored 100.0 and
# auto-approved. They are now refused up front and always routed to a human.
AUTO_MATCH_INELIGIBLE_TYPES: frozenset[str] = frozenset({
    "photo",
    "employee_id",
    "policy_ack",
    "legal_agreement",
    "onboarding",
    "asset_handover",
    "job_description",
    "closing_checklist",
})


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

    # 1. ADMIN-DEFINED LABELS FIRST. The Node backend owns the document-type
    #    catalogue and pushes it here (see set_dynamic_label_map). A label the
    #    catalogue knows about must resolve to exactly the schema the admin
    #    chose for it, including when that choice is 'generic'.
    #
    #    This is checked before the built-in tables below precisely because a
    #    catalogue label and a built-in alias can otherwise disagree: the
    #    built-ins were a snapshot of the old hard-coded list, and a seeded type
    #    whose label matches a built-in alias with a DIFFERENT schema would
    #    otherwise silently keep the stale mapping. The database is the source of
    #    truth now, so it goes first.
    dynamic = _dynamic_label_map()
    if key in dynamic:
        candidate = dynamic[key]
        # Guard the value, not the lookup: a schema_key that does not exist (a
        # typo, or a catalogue edited against a newer service build) falls back
        # to 'generic' instead of raising inside a request.
        return candidate if candidate in DOCUMENT_TYPE_SCHEMAS else "generic"

    # 2. Built-in resolution, unchanged, for labels this build already knows.
    if key in DOCUMENT_TYPE_SCHEMAS:
        return key
    resolved = DOCUMENT_TYPE_ALIASES.get(key)
    if resolved:
        return resolved
    # A caller may hand back a canonical key that was resolved earlier (that is
    # exactly what a cached field map carries). `_normalize_key` turns the
    # underscores back into spaces, so retry in the schema's own spelling before
    # giving up -- otherwise re-resolving a cache would silently degrade a typed
    # document to the generic schema and change which fields are REQUIRED.
    underscored = key.replace(" ", "_")
    if underscored in DOCUMENT_TYPE_SCHEMAS:
        return underscored
    if key == "generic":
        return "generic"
    return "generic"


def get_schema(document_type: str | None) -> tuple[str, dict[str, bool]]:
    """Return (canonical_key, schema_dict) for a document_type string."""
    key = resolve_document_type(document_type)
    return key, DOCUMENT_TYPE_SCHEMAS.get(key, GENERIC_SCHEMA)


def is_auto_match_eligible(document_type: str | None) -> tuple[bool, str]:
    """Whether this document type may ever produce an automatic approval.

    Returns ``(eligible, reason)``; the reason is a human-readable explanation
    suitable for the /match `reasons` array, and empty when the type is
    eligible. Accepts a raw catalog label or a canonical key, so callers do not
    have to resolve first.

    This is deliberately about the TYPE, not about the content: it is evaluated
    before extraction so an identity-poor type is refused without a comparison
    being run at all.
    """
    key = resolve_document_type(document_type)
    if key in AUTO_MATCH_INELIGIBLE_TYPES:
        return False, (
            f"document type '{key}' carries no identifying information, so a "
            "similarity score between two of them cannot establish that they "
            "belong to the same person; manual review is required"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Admin-defined catalogue (pushed from the Node backend)
# ---------------------------------------------------------------------------
#
# The document-type catalogue lives in the Node backend's `document_types`
# table so a system admin can add a type from the UI instead of editing code in
# two repositories. The backend pushes the catalogue here on every change (and
# at boot) through `PUT /schemas`.
#
# WHY AN HTTP PUSH RATHER THAN A SHARED DATABASE: this service is deliberately a
# stateless document processor. Giving it database credentials would couple it to
# the app's schema, duplicate the connection config in two places, and make it
# unable to start if the database is down -- for a process whose entire job is to
# render a PDF and run OCR. The push keeps it dependency-free (no MySQL driver in
# this venv) and keeps the database as the single source of truth.
#
# The map is held in memory and is intentionally volatile: if this process
# restarts before the next push, the service falls back to its built-in tables,
# which still resolve every seeded type correctly. A catalogue sync that never
# arrives degrades to the previous behaviour rather than to a failure.

_dynamic_labels: dict[str, str] = {}
_dynamic_lock = threading.Lock()


def _dynamic_label_map() -> dict[str, str]:
    """Snapshot of the pushed catalogue. A plain dict copy: the lock must not be
    held while a caller reads, or a long /match would block the next sync."""
    with _dynamic_lock:
        return dict(_dynamic_labels)


def set_dynamic_label_map(entries: list[dict] | None) -> dict:
    """Replace the pushed catalogue with `entries`.

    Each entry is ``{"label": <display name>, "schema_key": <canonical key>}``.
    REPLACES rather than merges, so a type deleted in the database stops
    resolving to its custom schema on the next sync instead of lingering as a
    stale alias forever.

    Returns a small report so the caller (and the audit log) can show what was
    actually taken:

      ``{"stored", "rejected"}``

    Entries naming a schema this build does not have are REJECTED rather than
    stored, and resolution falls back to 'generic' for them. Storing an unknown
    key would look like it worked while quietly extracting the wrong fields.
    """
    stored: dict[str, str] = {}
    rejected: list[dict] = []

    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        label = raw.get("label")
        schema_key = raw.get("schema_key")
        if not isinstance(label, str) or not label.strip():
            rejected.append({"label": label, "schema_key": schema_key, "reason": "missing label"})
            continue
        if not isinstance(schema_key, str) or schema_key not in DOCUMENT_TYPE_SCHEMAS:
            rejected.append(
                {
                    "label": label,
                    "schema_key": schema_key,
                    "reason": "unknown schema_key for this service build",
                }
            )
            continue
        stored[_normalize_key(label)] = schema_key

    with _dynamic_lock:
        _dynamic_labels.clear()
        _dynamic_labels.update(stored)

    return {"stored": len(stored), "rejected": rejected}


def describe_schemas() -> dict:
    """Machine-readable description of what this build can extract, for the
    admin UI's schema picker. Never includes catalogue data (that lives in the
    Node database) -- only the canonical schemas and which are auto-match
    ineligible."""
    return {
        "schemas": {
            key: {
                "fields": list(schema.keys()),
                "required": [f for f, req in schema.items() if req],
                "auto_match_eligible": key not in AUTO_MATCH_INELIGIBLE_TYPES,
            }
            for key, schema in DOCUMENT_TYPE_SCHEMAS.items()
        },
        "generic": {
            "fields": list(GENERIC_SCHEMA.keys()),
            "required": [f for f, req in GENERIC_SCHEMA.items() if req],
            "auto_match_eligible": True,
        },
        "auto_match_ineligible": sorted(AUTO_MATCH_INELIGIBLE_TYPES),
        "catalogue_count": len(_dynamic_label_map()),
    }