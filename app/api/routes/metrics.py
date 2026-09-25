"""GET /metrics — Prometheus-style in-memory counters. API-key protected."""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core import metrics

router = APIRouter()


@router.get("/metrics")
def metrics_endpoint() -> PlainTextResponse:
    """Expose per-endpoint request / failure / latency counters plus
    per-document-type outcome tallies in Prometheus text format.

    The output contains only endpoint paths, document_type keys and outcome
    labels — never extracted field values.
    """
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")