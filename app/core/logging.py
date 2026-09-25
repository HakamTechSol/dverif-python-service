"""Structured JSON logging for the Dvarif document service.

Every log line is a single JSON object written to stdout with request-level
metadata only — request id, endpoint, status, latency, document type and
outcome. The extracted name / CNIC values NEVER appear in logs: the OCR text
and per-field values stay entirely inside the route handlers, and the
observability middleware logs only identifiers and outcome booleans, never
document content.

(The pre-existing service had zero ``print`` / ``logging`` statements; the
logging added here deliberately receives only request metadata. See
tests/test_security.py for the regression guard that asserts extracted PII
never shows up in log output.)
"""

import json
import logging
import sys

_LOGGER_NAME = "dvarif.docservice"

# Extra attributes our observability middleware attaches to log records.
_LOG_FIELDS = (
    "request_id",
    "method",
    "path",
    "status",
    "duration_ms",
    "document_type",
    "outcome",
)


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line (no raw doc content)."""

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key in _LOG_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                line[key] = value
        return json.dumps(line, ensure_ascii=False)


def init_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Attach the JSON stdout handler once; idempotent across imports."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return the service logger without re-configuring it."""
    return logging.getLogger(name)