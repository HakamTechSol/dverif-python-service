"""Laya decision engine — advisory document classification for /ocr/extract.

WHAT THIS IS
    Laya (`laya` on PyPI, Convai Innovations) is a non-autoregressive decision
    model that answers typed questions about a text in one forward pass and
    returns calibrated probabilities per option. Here it is pointed at the OCR
    text of a document to answer three questions: what type of document this is,
    whether it reads as official, and whether it shows tampering.

WHY THE ENGINE IS ADVISORY, NOT A GATE
    Measured on this codebase's real document text (see the calibration notes on
    each question below), only `document_type` proved trustworthy. The other two
    are returned because they were asked for, but they are flagged
    `advisory_only` and nothing in this service or the Node backend acts on them.

        document_type        cnic 0.998 / payslip 0.99 / degree 0.906, and
                             "unknown" 0.70 for pure noise. Usable.

        is_official_document  Correct on the `english` checkpoint for Latin text
                             (NADRA CNIC -> 1.000, gibberish -> 0.000-0.088), but
                             on the `multilingual` checkpoint the same gibberish
                             scored 0.999 and a real Urdu CNIC scored 0.051. The
                             answer flips with the checkpoint, so it is not a
                             signal about the document. Advisory.

        document_risk         Does not separate real from fake. High-probability
                             was 0.65 for a tampered payslip but 0.33-0.58 for a
                             clean CNIC and 0.27-0.53 for random noise — clean
                             documents and noise overlap almost completely.
                             Advisory; using it to auto-reject would reject
                             legitimate employee documents.

    Laya itself warns at load time that this checkpoint ships out-of-range
    temperatures and that its confidence values should be treated as
    uncalibrated, which agrees with the measurements above.

WHY IT IS LAZY, CPU-PINNED AND OFF BY DEFAULT
    `Router(preload=True)` builds every checkpoint, which measured 6.3GB
    committed and 281s of blocking startup on a 4-core CPU box, and needs a
    2.26GB download. That is the opposite of the "no memory or CPU spike"
    requirement, so the router is constructed lazily with `max_loaded=1` and
    loads its checkpoint on first use (187MB idle).

    Even lazy, this is off by default (`LAYA_ENABLED=false`) because the latency
    measured here is not something to put inline on an interactive upload:
    building the first checkpoint from an already-warm HF cache took 60-124s,
    and warm inferences ranged 1.7-35s across runs. That 10x spread did not
    track the thread count (1 vs 2 vs 4 moved it in both directions), so the
    host is contended and no setting fixes it.

    Note the timeout self-heals: a call that times out mid-build leaves its
    worker running, and the checkpoint it finished building stays cached, so a
    later call on the same process is warm.

FAIL-OPEN
    Every entry point returns a dict and never raises. A missing checkpoint, no
    network, an OOM, a timeout or a transformers-version problem all produce
    {"available": false, ...} so that OCR extraction — the part that actually
    verifies identity — is never taken down by an advisory feature.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, TypeVar

from app.core.config import settings

logger = logging.getLogger(__name__)

# The three questions, in laya's own spec shape: `criteria` is a DICT of
# label -> description for a "choice" question, and "noul" takes no criteria.
# The `text` backticks tell laya which state key to read (see laya.presets).
QUESTIONS: dict[str, dict[str, Any]] = {
    "is_official_document": {
        "type": "noul",
        "instructions": (
            "Does `text` look like an official government-issued or employer-issued "
            "HR document (CNIC, salary slip, degree certificate, experience letter)?"
        ),
    },
    "document_risk": {
        "type": "choice",
        "instructions": "Does `text` show signs of tampering, forgery or fraud?",
        "criteria": {
            "low": "clean, internally consistent, correctly formatted, no anomalies",
            "high": "editing artifacts, mismatched numbers, altered text, layout tampering",
        },
    },
    "document_type": {
        "type": "choice",
        "instructions": "What kind of document is in `text`?",
        "criteria": {
            "cnic": "national identity card, CNIC, NIC, B-Form",
            "payslip": "salary slip, payslip, pay stub, wage statement",
            "degree": "degree certificate, transcript, degree, marksheet",
            "experience_letter": "experience letter, relieving letter, service letter",
            "unknown": "none of the above / cannot tell",
        },
    },
}

# Which of the three answers may be acted on. See the module docstring for the
# measurements behind this. Anything absent is advisory.
USABLE = frozenset({"document_type"})

T = TypeVar("T")

# Constructed at most once per process, guarded by _router_lock.
_router: Any = None
_router_lock = threading.Lock()

# Bounds concurrent inference: torch already parallelises inside a forward pass,
# so letting requests overlap only oversubscribes the cores and inflates tail
# latency. Rebuilt by reset_router() when the configured width changes.
_inference_gate: threading.Semaphore | None = None
_gate_width: int = -1


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    """The shape returned whenever the engine cannot produce an answer."""
    payload: dict[str, Any] = {"available": False, "reason": reason}
    payload.update(extra)
    return payload


def _gate() -> threading.Semaphore:
    """The concurrency semaphore, rebuilt if the configured width changed."""
    global _inference_gate, _gate_width
    width = max(1, settings.laya_max_concurrency)
    if _inference_gate is None or _gate_width != width:
        _inference_gate = threading.Semaphore(width)
        _gate_width = width
    return _inference_gate


def _get_router() -> Any:
    """Build the Router once, on first use. Imports laya only at this point."""
    global _router

    if _router is not None:
        return _router

    with _router_lock:
        if _router is not None:
            return _router

        # Keep torch off any GPU: this service has no GPU contract, and an
        # uninitialised CUDA context in a container is a common crash source.
        # Set before laya pulls in torch.
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        # Threading oversubscription during tokenisation is a classic source of
        # container CPU throttling, and laya has no reason to tokenise in parallel
        # for the single short document it is given here.
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

        from laya import Router  # imported here, not at module scope, on purpose

        if settings.laya_torch_threads > 0:
            # Process-global: this also retunes OCR and any other torch user.
            import torch

            torch.set_num_threads(settings.laya_torch_threads)

        router = Router(
            # NOT preload=True: that builds all three checkpoints, measured at
            # 6.3GB committed and ~281s of blocking startup. Lazy is 187MB idle.
            preload=settings.laya_preload,
            device=settings.laya_device,
            max_loaded=max(1, settings.laya_max_loaded),
        )
        _router = router
        logger.info(
            "laya router ready (device=%s, max_loaded=%s, preload=%s)",
            settings.laya_device,
            settings.laya_max_loaded,
            settings.laya_preload,
        )
        return _router


def reset_router() -> None:
    """Drop the cached router and gate. For tests, and after a config change."""
    global _router, _inference_gate, _gate_width
    with _router_lock:
        _router = None
        _inference_gate = None
        _gate_width = -1


def _call_within_budget(fn: Callable[[], T], budget_s: float) -> T:
    """Run `fn` on a worker thread and give up waiting after `budget_s`.

    Python cannot cancel a thread, so this bounds the WAIT, not the work: a
    timed-out call keeps its worker (and its CPU) until torch returns. That is
    acceptable only because the caller is advisory and fail-open, and because
    the concurrency gate stops further work from stacking on top of it.
    """
    if budget_s <= 0:
        return fn()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya-infer")
    try:
        future = pool.submit(fn)
        return future.result(timeout=budget_s)
    except FutureTimeout:
        # Do NOT wait for the worker: __exit__ would block on shutdown(wait=True)
        # and hand the caller back the very latency the budget exists to cap.
        pool.shutdown(wait=False, cancel_futures=True)
        raise


def _shape_answer(name: str, answer: dict[str, Any]) -> dict[str, Any]:
    """Normalise one raw laya answer into the shape the API contract exposes.

    laya answers a "noul" with {"noul": p} and a "choice" with
    {"choice": label, "probabilities": {...}}. Both are folded into
    value/probability so the Node backend has one shape to read.
    """
    qtype = answer.get("type")
    probabilities = answer.get("probabilities") or {}

    if qtype == "noul":
        p = float(answer.get("noul", 0.0))
        value: Any = p >= 0.5
        probability = p
    elif probabilities:
        value = answer.get("choice")
        probability = max(float(v) for v in probabilities.values())
    else:
        # No option distribution to compare against; report the model's own
        # confidence rather than inventing one.
        value = answer.get("choice")
        probability = float(answer.get("answer_confidence", answer.get("confidence", 0.0)) or 0.0)

    return {
        "type": qtype,
        "value": value,
        "probability": round(float(probability), 4),
        "probabilities": {k: round(float(v), 4) for k, v in probabilities.items()},
        "confidence": round(float(answer.get("answer_confidence", probability) or 0.0), 4),
        "advisory_only": name not in USABLE,
    }


def score_document(text: str) -> dict[str, Any]:
    """Score document `text` with Laya. Never raises; see the module docstring.

    Returns {"available": false, "reason": ...} when the engine is disabled, has
    no text to read, cannot load, or does not answer within its budget.
    """
    if not settings.laya_enabled:
        return _unavailable("disabled", skipped=True)

    if not text or not text.strip():
        return _unavailable("empty_text", skipped=True)

    try:
        router = _get_router()
    except Exception as exc:  # import failure, no torch, bad config
        logger.warning("laya: router unavailable: %s", exc)
        return _unavailable("router_unavailable", detail=str(exc)[:300])

    state_text = text[: settings.laya_max_chars]
    truncated = len(text) > settings.laya_max_chars
    budget = settings.laya_timeout_s

    started = time.perf_counter()
    gate = _gate()
    if not gate.acquire(timeout=budget if budget > 0 else None):
        return _unavailable("busy", timeout_s=budget)
    try:
        # The gate wait counts against the caller's budget, so re-derive what is
        # left before spending it on torch.
        remaining = budget - (time.perf_counter() - started)
        result = _call_within_budget(
            lambda: router.predict({"text": state_text}, QUESTIONS), remaining
        )
    except FutureTimeout:
        logger.warning("laya: inference exceeded its %.1fs budget", budget)
        return _unavailable("timeout", timeout_s=budget)
    except Exception as exc:
        # Most likely no network for the checkpoint download, or an OOM. The
        # router stays cached, so a later request retries the load.
        logger.warning("laya: inference failed: %s", exc)
        return _unavailable("inference_failed", detail=str(exc)[:300])
    finally:
        gate.release()

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    answers = result.get("answers") or {}
    routing = result.get("routing") or {}

    return {
        "available": True,
        "model": result.get("model"),
        "checkpoint": routing.get("model"),
        "routed_by": routing.get("reason"),
        "elapsed_ms": elapsed_ms,
        "truncated": truncated,
        "answers": {
            name: _shape_answer(name, answers[name])
            for name in QUESTIONS
            if name in answers
        },
        "usage": result.get("usage") or {},
    }