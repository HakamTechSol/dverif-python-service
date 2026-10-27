"""Tests for the advisory Laya engine and its /ocr/extract integration.

No checkpoint is downloaded here. The router is stubbed so these assert the
CONTRACT (shape, fail-open behaviour, advisory flags, budget, truncation), which
is the part that can regress. The calibration of the model itself was measured
out-of-band and is recorded in the engine's docstring.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import laya_engine

client = TestClient(app)
HEADERS = {"X-API-Key": "test-api-key"}

RAW_RESULT = {
    "model": "laya-rl-agent",
    "answers": {
        "is_official_document": {"type": "noul", "noul": 0.926, "answer_confidence": 0.074},
        "document_risk": {
            "type": "choice",
            "choice": "low",
            "probabilities": {"low": 0.6713, "high": 0.3287},
            "confidence": 0.086,
            "answer_confidence": 0.6713,
        },
        "document_type": {
            "type": "choice",
            "choice": "cnic",
            "probabilities": {"cnic": 0.998, "payslip": 0.0, "degree": 0.0,
                              "experience_letter": 0.0, "unknown": 0.002},
            "confidence": 1.0,
            "answer_confidence": 0.998,
        },
    },
    "usage": {"input_tokens": 388, "state_tokens": 67, "truncated": False},
    "routing": {"model": "multilingual", "reason": "explicit model='multilingual'"},
}


class StubRouter:
    """Stands in for laya.Router. Records what it was asked."""

    def __init__(self, result=None, raises=None, delay=0.0):
        self.result = result if result is not None else RAW_RESULT
        self.raises = raises
        self.delay = delay
        self.calls = []

    def predict(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if self.delay:
            threading.Event().wait(self.delay)
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def router(monkeypatch):
    """Install a stub router and turn the feature on for one test."""
    stub = StubRouter()
    monkeypatch.setattr(settings, "laya_enabled", True)
    monkeypatch.setattr(laya_engine, "_get_router", lambda: stub)
    laya_engine.reset_router()
    yield stub
    laya_engine.reset_router()


# --- question spec ---------------------------------------------------------

def test_questions_match_laya_spec_shape():
    """criteria is a dict for choice, absent for noul. laya rejects anything else."""
    assert laya_engine.QUESTIONS["is_official_document"]["type"] == "noul"
    assert "criteria" not in laya_engine.QUESTIONS["is_official_document"]

    for name in ("document_risk", "document_type"):
        spec = laya_engine.QUESTIONS[name]
        assert spec["type"] == "choice"
        assert isinstance(spec["criteria"], dict)
        assert all(isinstance(v, str) and v for v in spec["criteria"].values())
        # Every instruction must name the state key it reads.
        assert "`text`" in spec["instructions"]


def test_only_document_type_is_marked_usable():
    """The two uncalibrated questions must never be actable on."""
    assert laya_engine.USABLE == frozenset({"document_type"})


# --- happy path ------------------------------------------------------------

def test_scores_all_three_questions(router):
    out = laya_engine.score_document("National Identity Card NADRA CNIC 42101-1234567-9")

    assert out["available"] is True
    assert out["checkpoint"] == "multilingual"
    assert set(out["answers"]) == set(laya_engine.QUESTIONS)
    assert out["elapsed_ms"] >= 0


def test_noul_is_folded_into_a_bool(router):
    answer = laya_engine.score_document("CNIC 42101-1234567-9")["answers"]["is_official_document"]
    assert answer["type"] == "noul"
    assert answer["value"] is True          # 0.926 >= 0.5
    assert answer["probability"] == 0.926
    assert answer["probabilities"] == {}    # a noul has no option distribution


def test_choice_keeps_its_full_probability_distribution(router):
    answer = laya_engine.score_document("CNIC 42101-1234567-9")["answers"]["document_type"]
    assert answer["value"] == "cnic"
    assert answer["probability"] == 0.998
    assert set(answer["probabilities"]) == {
        "cnic", "payslip", "degree", "experience_letter", "unknown"
    }


def test_advisory_flag_is_set_on_both_uncalibrated_answers(router):
    answers = laya_engine.score_document("some payslip text")["answers"]
    assert answers["document_type"]["advisory_only"] is False
    assert answers["document_risk"]["advisory_only"] is True
    assert answers["is_official_document"]["advisory_only"] is True


def test_state_is_passed_under_the_text_key(router):
    laya_engine.score_document("CNIC 42101-1234567-9")
    assert router.calls[0]["state"] == {"text": "CNIC 42101-1234567-9"}
    assert router.calls[0]["questions"] is laya_engine.QUESTIONS


# --- input bounding --------------------------------------------------------

def test_long_text_is_truncated_and_flagged(router, monkeypatch):
    monkeypatch.setattr(settings, "laya_max_chars", 50)
    out = laya_engine.score_document("x" * 5000)

    assert out["truncated"] is True
    assert len(router.calls[0]["state"]["text"]) == 50


def test_short_text_is_not_truncated(router, monkeypatch):
    monkeypatch.setattr(settings, "laya_max_chars", 5000)
    assert laya_engine.score_document("short")["truncated"] is False


# --- fail-open -------------------------------------------------------------

def test_disabled_engine_skips_without_touching_the_router():
    """settings.laya_enabled is False across the suite (see conftest)."""
    assert settings.laya_enabled is False
    out = laya_engine.score_document("CNIC 42101-1234567-9")
    assert out == {"available": False, "reason": "disabled", "skipped": True}


def test_empty_text_skips_inference(router):
    for blank in ("", "   ", "\n\t"):
        assert laya_engine.score_document(blank)["reason"] == "empty_text"
    assert router.calls == []


def test_inference_failure_is_swallowed(router):
    router.raises = RuntimeError("CUDA out of memory")
    out = laya_engine.score_document("CNIC 42101-1234567-9")

    assert out["available"] is False
    assert out["reason"] == "inference_failed"
    assert "CUDA out of memory" in out["detail"]


def test_missing_checkpoint_download_is_swallowed(monkeypatch):
    """No network: the router cannot be built, and extraction still continues."""
    def boom():
        raise OSError("could not resolve host huggingface.co")

    monkeypatch.setattr(settings, "laya_enabled", True)
    monkeypatch.setattr(laya_engine, "_get_router", boom)
    out = laya_engine.score_document("CNIC 42101-1234567-9")

    assert out["available"] is False
    assert out["reason"] == "router_unavailable"


def test_over_budget_inference_times_out(router, monkeypatch):
    monkeypatch.setattr(settings, "laya_timeout_s", 0.05)
    router.delay = 2.0

    out = laya_engine.score_document("CNIC 42101-1234567-9")
    assert out["available"] is False
    assert out["reason"] == "timeout"
    assert out["timeout_s"] == 0.05


def test_gate_is_released_after_a_timeout(router, monkeypatch):
    """A timed-out call must not permanently occupy the concurrency slot."""
    monkeypatch.setattr(settings, "laya_timeout_s", 0.05)
    router.delay = 0.4
    assert laya_engine.score_document("x")["reason"] == "timeout"
    # If the gate leaked, this second call could never acquire.
    assert laya_engine._gate().acquire(timeout=1.0) is True
    laya_engine._gate().release()


# --- endpoint integration --------------------------------------------------

def test_ocr_extract_reports_the_engine_as_disabled(monkeypatch):
    """With Laya off the endpoint keeps its exact pre-Laya shape plus `laya`."""
    monkeypatch.setattr("app.api.routes.ocr.extractText", lambda _p: "CNIC Number: 42101-1234567-1")
    import io

    res = client.post(
        "/ocr/extract",
        files={"file": ("d.txt", io.BytesIO(b"ignored"), "text/plain")},
        data={"document_type": "cnic"},
        headers=HEADERS,
    )
    body = res.json()
    assert res.status_code == 200
    assert body["success"] is True
    assert body["data"]["fields"]                       # extraction still works
    assert body["data"]["laya"] == {
        "available": False, "reason": "disabled", "skipped": True
    }


def test_ocr_extract_attaches_live_answers(monkeypatch):
    monkeypatch.setattr(settings, "laya_enabled", True)
    monkeypatch.setattr("app.api.routes.ocr.extractText", lambda _p: "CNIC 42101-1234567-9")
    monkeypatch.setattr(laya_engine, "_get_router", lambda: StubRouter())
    import io

    res = client.post(
        "/ocr/extract",
        files={"file": ("d.txt", io.BytesIO(b"ignored"), "text/plain")},
        data={"document_type": "cnic"},
        headers=HEADERS,
    )
    laya = res.json()["data"]["laya"]
    assert laya["available"] is True
    assert laya["answers"]["document_type"]["value"] == "cnic"
    assert laya["answers"]["document_type"]["advisory_only"] is False


def test_ocr_extract_still_succeeds_when_laya_explodes(monkeypatch):
    """The whole point of fail-open: a broken engine cannot fail a verification."""
    monkeypatch.setattr(settings, "laya_enabled", True)
    monkeypatch.setattr("app.api.routes.ocr.extractText", lambda _p: "CNIC Number: 42101-1234567-1")
    monkeypatch.setattr(
        laya_engine, "_get_router", lambda: StubRouter(raises=RuntimeError("checkpoint gone"))
    )
    import io

    res = client.post(
        "/ocr/extract",
        files={"file": ("d.txt", io.BytesIO(b"ignored"), "text/plain")},
        data={"document_type": "cnic"},
        headers=HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["data"]["laya"]["reason"] == "inference_failed"