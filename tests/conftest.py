"""Shared test bootstrap.

Env is set BEFORE any ``app.*`` import so the pydantic-settings singleton the
routes read is always built with deterministic test values, regardless of the
collection/import order of the individual test modules.
"""

import os

os.environ["DOC_SERVICE_API_KEY"] = "test-api-key"
os.environ["MAX_UPLOAD_SIZE_MB"] = "1"
os.environ["HOST"] = "127.0.0.1"