from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://invoices:invoices@localhost:5435/invoices"

    # Mailbox the AP team forwards supplier invoices to
    imap_host: str = "localhost"
    imap_port: int = 3143
    imap_ssl: bool = False
    imap_user: str = "ap@tembusu-housewares.example"
    imap_password: str = "ap-password"
    imap_folder: str = "INBOX"
    poll_seconds: int = 15

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "invoice-flow"

    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    dlq_name: str = "invoices.dlq"

    # Extraction: "llm" uses Claude with a strict schema (cheap model first, strong model if the
    # cheap one fails validation); "rules" parses the bundled synthetic layouts offline (demo/CI).
    extractor: str = "llm"
    extract_model: str = "claude-haiku-5-5"
    extract_fallback_model: str = "claude-opus-5-5"

    # OCR for scanned PDFs (no text layer). Textract when AWS credentials are present.
    textract_enabled: bool = False
    aws_region: str = "ap-southeast-1"

    # ERP MCP Bridge (Project 1)
    bridge_url: str = "http://localhost:8000"
    bridge_client_id: str = "invoice-flow"
    bridge_client_secret: str = "invoice-flow-demo-secret"

    human_review_timeout_hours: int = 72
    public_url: str = "http://localhost:8200"


@lru_cache
def get_settings() -> Settings:
    return Settings()
