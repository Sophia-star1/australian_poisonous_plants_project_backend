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

    # --- CORS ---
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]

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
    def celery_enabled(self) -> bool:
        """Check whether Celery broker is configured."""
        return bool(self.CELERY_BROKER_URL)


settings = Settings()
