"""In-memory Prometheus-style counters, exposed at GET /metrics.

Tracks per-endpoint request count, failure rate (status >= 400) and latency
(a summary), plus structured outcomes keyed by
(endpoint, document_type, outcome) — e.g.

    document_service_outcome_total{endpoint="/match",document_type="cnic",outcome="match"} 3

so downstream analytics can break down approval vs fallback behaviour by
document type (cnic vs offer_letter vs ... without ever seeing the values).

The registry is intentionally an in-process dict: single uvicorn worker, no
external dependencies. `reset()` exists for tests.
"""

import threading

_lock = threading.Lock()
_requests_total: dict[str, int] = {}
_failures_total: dict[str, int] = {}
_duration: dict[str, list] = {}  # path -> [count, sum_seconds]
_outcomes: dict[tuple, int] = {}


def record_request(path: str, status: int, duration_s: float) -> None:
    """Tally one completed request (any status, incl. auth/flow failures)."""
    with _lock:
        _requests_total[path] = _requests_total.get(path, 0) + 1
        if status >= 400:
            _failures_total[path] = _failures_total.get(path, 0) + 1
        count, total = _duration.get(path, [0, 0.0])
        _duration[path] = [count + 1, total + duration_s]


def record_outcome(path: str, document_type: str, outcome: str) -> None:
    """Tally a document-level outcome (match/no_match/ok) for a doc type."""
    key = (path, document_type, outcome)
    with _lock:
        _outcomes[key] = _outcomes.get(key, 0) + 1


def reset() -> None:
    """Clear every counter (test helper)."""
    with _lock:
        _requests_total.clear()
        _failures_total.clear()
        _duration.clear()
        _outcomes.clear()


def _label_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    """Prometheus text exposition for the current registry state."""
    with _lock:
        lines = [
            "# TYPE document_service_requests_total counter",
        ]
        for path, count in sorted(_requests_total.items()):
            lines.append(f'document_service_requests_total{{endpoint="{path}"}} {count}')

        lines.append("# TYPE document_service_failures_total counter")
        for path, count in sorted(_failures_total.items()):
            lines.append(f'document_service_failures_total{{endpoint="{path}"}} {count}')

        lines.append("# TYPE document_service_request_duration_seconds summary")
        for path in sorted(_duration):
            count, total = _duration[path]
            lines.append(
                f'document_service_request_duration_seconds_count{{endpoint="{path}"}} {count}'
            )
            lines.append(
                f'document_service_request_duration_seconds_sum{{endpoint="{path}"}} {total:g}'
            )

        lines.append("# TYPE document_service_outcome_total counter")
        for (path, doc_type, outcome) in sorted(_outcomes):
            lines.append(
                'document_service_outcome_total{endpoint="%s",document_type="%s",outcome="%s"} %d'
                % (_label_escape(path), _label_escape(doc_type), _label_escape(outcome), _outcomes[(path, doc_type, outcome)])
            )

        return "\n".join(lines) + "\n"