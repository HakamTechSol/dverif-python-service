"""Shared test bootstrap.

Env is set BEFORE any ``app.*`` import so the pydantic-settings singleton the
routes read is always built with deterministic test values, regardless of the
collection/import order of the individual test modules.
"""

import os

os.environ["DOC_SERVICE_API_KEY"] = "test-api-key"
os.environ["MAX_UPLOAD_SIZE_MB"] = "1"
os.environ["HOST"] = "127.0.0.1"

# The Laya engine builds a ~0.6-0.8GB checkpoint (60-124s measured) and takes
# seconds per document on CPU, so it is off for the whole suite — which is also
# the shipped default (LAYA_ENABLED=false). Tests that exercise the engine itself
# (tests/test_laya_engine.py) enable it against a stubbed router; the endpoint
# test asserts the "disabled" shape instead.
os.environ["LAYA_ENABLED"] = "false"