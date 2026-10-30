import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    database_url: str
    webhook_secret: str
    message_id_path: str | None
    campaign_id_path: str | None
    max_body_bytes: int
    max_batch_records: int

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            webhook_secret=os.getenv("KUMO_WEBHOOK_SECRET", ""),
            # Intentionally unset until the real KumoMTA payload is verified.
            message_id_path=os.getenv("KUMO_MESSAGE_ID_PATH") or None,
            campaign_id_path=os.getenv("KUMO_CAMPAIGN_ID_PATH") or None,
            max_body_bytes=int(os.getenv("INGEST_MAX_BODY_BYTES", "1048576")),
            max_batch_records=int(os.getenv("INGEST_MAX_BATCH_RECORDS", "500")),
        )

def get_settings():
    s = Settings.from_env()
    if not s.database_url:
        raise RuntimeError("DATABASE_URL is required")
    if not s.webhook_secret:
        raise RuntimeError("KUMO_WEBHOOK_SECRET is required")
    if s.max_body_bytes < 1 or s.max_batch_records < 1:
        raise RuntimeError("Ingestion limits must be positive")
    return s