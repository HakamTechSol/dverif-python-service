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


settings = Settings()