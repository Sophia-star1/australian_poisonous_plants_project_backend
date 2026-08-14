"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core ---
    APP_NAME: str = "apl-backend"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # --- Database (required) ---
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/apl_db"

    # --- Redis (optional) ---
    REDIS_URL: str = ""

    # --- Object Storage (optional) ---
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_S3_ENDPOINT_URL: str = ""

    # --- Supabase Storage ---
    #: Project URL, e.g. https://<ref>.supabase.co
    SUPABASE_URL: str = ""
    #: Service-role key. Server-side only — it bypasses row level security,
    #: so it must never reach the mobile client or a browser bundle.
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    #: Where contributor photos land while a submission awaits review.
    SUPABASE_SUBMISSION_BUCKET: str = "submission-images"
    #: Where approved photos are promoted to, alongside the curated library.
    SUPABASE_PLANT_BUCKET: str = "plant-images"
    #: Rejected uploads, per policy: keep the originals as an audit trail.
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    ALLOWED_IMAGE_TYPES: list[str] = ["image/jpeg", "image/png", "image/webp"]

    # --- CORS ---
    ALLOWED_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:8081",
        "http://localhost:19006",
    ]

    # --- Rate limiting (credential endpoints) ---
    #: Attempts allowed per window, counted per client IP and per account.
    LOGIN_RATE_LIMIT: int = 10
    SIGNUP_RATE_LIMIT: int = 5
    RATE_LIMIT_WINDOW_SECONDS: int = 300
    #: Escape hatch for load tests and local scripting. Never enable in prod.
    RATE_LIMIT_ENABLED: bool = True

    # --- Celery (optional) ---
    CELERY_BROKER_URL: str = ""

    @property
    def redis_enabled(self) -> bool:
        """Check whether Redis is configured."""
        return bool(self.REDIS_URL)

    @property
    def storage_enabled(self) -> bool:
        """Check whether object storage is configured."""
        return bool(self.AWS_ACCESS_KEY_ID and self.AWS_S3_BUCKET)

    @property
    def supabase_storage_enabled(self) -> bool:
        """Check whether Supabase Storage credentials are present."""
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY)

    @property
    def celery_enabled(self) -> bool:
        """Check whether Celery broker is configured."""
        return bool(self.CELERY_BROKER_URL)


settings = Settings()
