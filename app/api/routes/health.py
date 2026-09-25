"""GET /health — liveness probe. No API key required."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    """Liveness probe — no API key required."""
    return {"status": "ok"}