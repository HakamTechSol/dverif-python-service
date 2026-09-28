"""GET/PUT /schemas — the document-type catalogue this service can resolve.

The catalogue itself (which document types exist) lives in the Node backend's
`document_types` table, because that is what a system admin edits. This service
only needs to know how a LABEL maps to a canonical field schema, so the backend
pushes that mapping here and this service resolves against it.

Both routes are covered by the global API-key middleware (app.main), so they
are reachable only from the backend, not from a browser.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.document_schemas import (
    describe_schemas,
    resolve_document_type,
    set_dynamic_label_map,
)

router = APIRouter()

# The catalogue is small (tens of rows) and is replaced wholesale, so a cap here
# is purely a guard against a caller pushing an unbounded payload.
MAX_CATALOGUE_ENTRIES = 500


class CatalogueEntry(BaseModel):
    label: str
    schema_key: str


class CataloguePayload(BaseModel):
    types: list[CatalogueEntry] = Field(default_factory=list)


@router.get("/schemas")
def get_schemas():
    """Describe the canonical schemas this build supports.

    Used by the admin UI to populate the schema picker when adding or editing a
    document type, so an admin can only pick a schema that will actually work.
    """
    return {"success": True, "data": describe_schemas()}


@router.put("/schemas")
def put_schemas(payload: CataloguePayload):
    """Replace the label -> schema mapping with the database's current state.

    REPLACES, never merges: a document type deleted in the database must stop
    resolving to its custom schema, or it would linger as a stale alias for as
    long as this process lives.

    Entries naming a schema this build does not know are rejected individually
    and reported back, so a half-applied sync is visible rather than silent. A
    rejected entry resolves to 'generic', which is a safe schema, so OCR keeps
    working for that type.
    """
    if len(payload.types) > MAX_CATALOGUE_ENTRIES:
        raise HTTPException(
            status_code=400,
            detail=f"too many document types (max {MAX_CATALOGUE_ENTRIES})",
        )

    report = set_dynamic_label_map([t.model_dump() for t in payload.types])

    return {
        "success": True,
        "data": {
            "stored": report["stored"],
            "rejected_count": len(report["rejected"]),
            "rejected": report["rejected"],
        },
    }


@router.post("/schemas/resolve")
def post_resolve(payload: CataloguePayload):
    """Report how each label resolves against the CURRENT catalogue.

    Deliberately does NOT push the payload. An endpoint named "resolve" that
    silently installs whatever it is given is a footgun: it cannot be used to
    ask "does the service still know about this label?", because asking the
    question is itself the thing that makes the answer true. To sync and then
    verify, PUT /schemas first, then POST this.
    """
    return {
        "success": True,
        "data": {
            "resolved": [
                {"label": t.label, "schema_key": resolve_document_type(t.label)}
                for t in payload.types
            ]
        },
    }
