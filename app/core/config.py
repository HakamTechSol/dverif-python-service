"""Central configuration for the Dvarif document service.

Reads settings from the .env file in the python-backend folder (or the
process environment). Moved from the top-level Settings class in app.py;
hardened since: upload cap now defaults to 15MB and the server binds to
127.0.0.1 unless HOST=0.0.0.0 is explicitly configured for deployment.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, sourced from env vars / the .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    doc_service_api_key: str = ""

    # Per-file upload cap in MB. 15 is slightly above the Node.js backend's own
    # 10MB multer limit so legitimate forwarded files are never rejected here.
    max_upload_size_mb: int = 15

    # Bind to loopback by default; bind 0.0.0.0 (all interfaces) ONLY when an
    # operator explicitly sets HOST=0.0.0.0 in the environment / .env for a
    # real deployment (e.g. behind a reverse proxy on a different machine).
    host: str = "127.0.0.1"
    port: int = 5001

    # --- Laya decision engine (app/services/laya_engine.py) -----------------
    # Everything here is opt-in. Two independent reasons, both measured:
    #
    #  1. Latency. Building the first checkpoint from cache took 60-124s and a
    #     warm inference ran 1.7-35s on CPU — a 10x spread across runs that
    #     thread-pinning did not explain, so the host is contended. That does
    #     not belong in the inline path of an interactive verification upload.
    #  2. Cost. It imports torch and pulls ~0.6-0.8GB of weights from
    #     HuggingFace on first use.
    #
    # While disabled, /ocr/extract returns no `laya` key beyond the stub
    # {"available": false, "reason": "disabled"} and costs nothing. Enable it on
    # a host that can absorb the latency (see LAYA_TIMEOUT_S) or behind the
    # async queue the service does not yet have.
    laya_enabled: bool = False

    # Resident checkpoints. Measured on a 4-core CPU box:
    #   max_loaded=1 + preload=False -> 187MB RSS while idle
    #   preload=True (all 3)         -> 6.3GB committed, 281s of blocking startup
    # 1 is the only value that matches "no high memory spikes", so it is the
    # default. Raise it only if a deployment can afford a second resident
    # checkpoint (~0.8GB each) to avoid re-loading on a script switch.
    laya_max_loaded: int = 1

    # Preload all checkpoints at startup. Off by default: it blocks startup for
    # minutes and commits every checkpoint at once. Only useful when the host is
    # known to have the RAM and a cold first request is worse than a slow boot.
    laya_preload: bool = False

    # "cpu" is the default because this service ships without a GPU contract and
    # a CUDA build would silently fall back anyway. Laya itself falls back to
    # CPU when CUDA is unavailable, so "cuda" here is a preference, not a promise.
    laya_device: str = "cpu"

    # Upper bound on the characters handed to the model. OCR of a dense page
    # produces far more text than a decision model benefits from, and cost on CPU
    # is linear in tokens. 6000 chars is ~1500 tokens.
    laya_max_chars: int = 6000

    # Concurrent inferences allowed. 1 serialises them: torch already uses
    # several intra-op threads per forward pass, so letting requests overlap just
    # oversubscribes the cores and inflates tail latency without adding throughput.
    laya_max_concurrency: int = 1

    # Wall-clock ceiling for one inference, enforced here in-process so a slow
    # cold start cannot pin a request handler indefinitely. Note that a thread
    # blocked in torch cannot be cancelled, so this bounds the wait, not the
    # work: a timed-out call leaves its worker running until torch returns, and
    # the checkpoint it was building stays cached, so the next request is warm.
    # 30s covers the measured warm range; it will NOT cover a 60-124s first
    # build, which therefore times out and succeeds on a later call.
    laya_timeout_s: float = 30.0

    # torch.set_num_threads is PROCESS-GLOBAL, so setting it from here also
    # changes OCR and every other torch user in the process. 0 leaves torch's
    # own default alone, which is the right call: measured 1 vs 2 vs 4 threads
    # moved latency by 10x in both directions on this host and the cause was
    # contention, not the setting.
    laya_torch_threads: int = 0


settings = Settings()