"""Tests for the python document service hardening:

- constant-time API-key comparison (wrong / missing key rejected; /health stays public)
- per-file upload cap enforced BOTH up front (from Content-Length) and while
  streaming (`_persist_upload`)

Deterministic cap: the module-overridden MAX_UPLOAD_SIZE_MB=1 makes the tests
fast, and a dedicated test re-checks the shipped defaults on a fresh instance.
"""

import io
import os
import tempfile

# Set a small, deterministic cap BEFORE anything imports app.core.config, so the
# module-level settings singleton the routes read is predictable in this process.
os.environ["DOC_SERVICE_API_KEY"] = "test-api-key"  # overrides the real one in .env
os.environ["MAX_UPLOAD_SIZE_MB"] = "1"
os.environ["HOST"] = "127.0.0.1"

import pytest  # noqa: E402
from fastapi import HTTPException, UploadFile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import Settings, settings  # noqa: E402
from app.main import app  # noqa: E402
from app.utils.file_handling import _coarse_allowed_bytes, _persist_upload, upload_size_limit_bytes  # noqa: E402

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}
LIMIT = upload_size_limit_bytes()  # 1 MiB in this process
SLACK = 64 * 1024


# --- config defaults ----------------------------------------------------------


def test_config_env_override_took_effect():
    assert LIMIT == 1 * 1024 * 1024
    assert settings.max_upload_size_mb == 1
    assert settings.host == "127.0.0.1"


def test_config_defaults_are_loopback_and_15mb():
    saved = {key: os.environ.get(key) for key in ("HOST", "MAX_UPLOAD_SIZE_MB")}
    for key in saved:
        os.environ.pop(key, None)
    try:
        fresh = Settings()
        assert fresh.host == "127.0.0.1"  # never 0.0.0.0 by default
        assert fresh.max_upload_size_mb == 15
        assert fresh.port == 5001
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


# --- auth ---------------------------------------------------------------------


def test_health_requires_no_key():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_validate_rejects_missing_key():
    resp = client.post("/validate", files={"file": ("a.txt", b"hello", "text/plain")})
    assert resp.status_code == 401


def test_validate_rejects_wrong_key():
    resp = client.post(
        "/validate",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers={"X-API-Key": "definitely-not-the-key"},
    )
    assert resp.status_code == 401


def test_validate_accepts_correct_key():
    resp = client.post(
        "/validate",
        files={"file": ("a.txt", b"hello world", "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["valid"] is True


# --- upload size enforcement --------------------------------------------------


def test_coarse_allowed_bytes_per_route():
    assert _coarse_allowed_bytes("/validate") == LIMIT + SLACK
    assert _coarse_allowed_bytes("/match") == 2 * LIMIT + SLACK


def test_precheck_rejects_oversized_raw_body():
    # A raw body larger than the per-file ceiling + slack trips the
    # Content-Length pre-check before FastAPI even parses the request.
    body = b"x" * (LIMIT + SLACK + 1)
    resp = client.post("/validate", content=body, headers=HEADERS)
    assert resp.status_code == 413
    assert resp.json()["message"] == "Upload exceeds the 1 MB limit"


def test_streaming_cap_catches_file_over_limit_within_slack():
    # Multipart overhead keeps the request body under the coarse ceiling even
    # though the FILE exceeds the cap — so the pre-check passes and only the
    # streaming write in _persist_upload rejects it (413, connection closed).
    oversized = b"z" * (LIMIT + 1024)
    resp = client.post(
        "/validate",
        files={"file": ("big.pdf", io.BytesIO(oversized), "application/pdf")},
        headers=HEADERS,
    )
    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"]


def test_file_exactly_at_cap_is_accepted():
    # A file exactly AT the per-file cap (not over) must never be rejected by
    # either the pre-check or the streaming write.
    at_cap = b"t" * LIMIT
    resp = client.post(
        "/validate",
        files={"file": ("at-cap.txt", io.BytesIO(at_cap), "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["valid"] is True


def test_match_allows_two_files_up_to_the_doubled_ceiling():
    # Two 700KB files = ~1.4MB body: over the single-file ceiling but under
    # the /match double ceiling, so both pre-check AND per-file streaming pass.
    payload = b"t" * (700 * 1024)
    resp = client.post(
        "/match",
        files={
            "file_a": ("a.txt", io.BytesIO(payload), "text/plain"),
            "file_b": ("b.txt", io.BytesIO(payload), "text/plain"),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_persist_upload_writes_file_and_persists():
    small = UploadFile(filename="ok.txt", file=io.BytesIO(b"hello"))
    path = _persist_upload(small)
    try:
        assert os.path.exists(path)
        assert path.endswith(".txt")
        with open(path, "rb") as fh:
            assert fh.read() == b"hello"
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_persist_upload_streaming_cap_cleans_up_temp_file(monkeypatch):
    real_mkstemp = tempfile.mkstemp
    created_paths = []

    def fake_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created_paths.append(path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", fake_mkstemp)

    oversized = UploadFile(filename="big.pdf", file=io.BytesIO(b"z" * (LIMIT + 60)))
    with pytest.raises(HTTPException) as excinfo:
        _persist_upload(oversized)
    assert excinfo.value.status_code == 413
    assert len(created_paths) == 1
    assert not os.path.exists(created_paths[0])