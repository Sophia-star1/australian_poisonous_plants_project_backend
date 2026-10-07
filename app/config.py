"""Application configuration via pydantic-settings."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator(
        "DATABASE_URL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_S3_ENDPOINT_URL",
        mode="after",
    )
    @classmethod
    def _strip_surrounding_whitespace(cls, value: str) -> str:
        """Trim whitespace from credentials and connection strings.

        Secret Manager stores exactly the bytes it was given, and the usual
        ways of loading one append a newline — `printf` without `%s`, a here
        doc, or a PowerShell pipe into `--data-file=-`. The newline then
        travels into an HTTP header, where httpx rejects the value outright:

            LocalProtocolError: Illegal header value b'Bearer sb_secret_...\\n'

        That surfaced as a bare HTTP 500 on every photo upload with nothing in
        the response to suggest a misconfigured secret. Whitespace carries no
        meaning in any of these values, so stripping it is safe and removes a
        failure mode that costs far more to diagnose than to prevent.

        SECRET_KEY is deliberately excluded: it is opaque HMAC key material
        where a trailing newline is harmless, and trimming it would invalidate
        every token signed with the untrimmed value the moment this deploys.
        """
        return value.strip()

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
    #: `plant-images-v2` to match `plant_images_v2`: the curated v2 rows all
    #: point at that bucket, and promoted photos belong next to the library
    #: they are joined to, not in the v1 bucket the app no longer reads.
    SUPABASE_PLANT_BUCKET: str = "plant-images-v2"
    #: Rejected uploads, per policy: keep the originals as an audit trail.
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    ALLOWED_IMAGE_TYPES: list[str] = ["image/jpeg", "image/png", "image/webp"]

    # --- Plant screening ---
    #: Off switch. The classifier also stays out of the way on its own when
    #: the weights are missing, so a checkout without them still boots.
    PLANT_CLASSIFIER_ENABLED: bool = True
    #: Below this the upload is refused. Raised from 0.10 because ordinary
    #: non-plants landed above that floor and were accepted: screenshots
    #: score 0.24–0.49, a blank frame about 0.32. The cost is honest photos:
    #: against this project's curated library — 104 photographs, all
    #: genuinely plants — 0.5 turns away 6 of them (0.10 turned away 1, the
    #: model's own 0.7 cutoff 7). Those are specimen shots, bark close-ups and
    #: non-green plant parts, and since every photo in a submission must pass,
    #: the chance of refusing an honest submission grows with its photo count.
    PLANT_REJECT_BELOW: float = 0.50
    #: At or above this the photo counts as a plant. Anything in between is
    #: accepted but marked for the reviewer rather than refused.
    PLANT_ACCEPT_AT: float = 0.70
    #: How many photos may be decoded and scored at the same time.
    #: Decoding is where the memory goes — measured at ~70 MB for one 1919x2560
    #: photograph — and Cloud Run defaults to 80 concurrent requests per
    #: instance. Unbounded, a burst of uploads reaches several GB and the
    #: instance is killed, taking every in-flight request with it. Bounding
    #: the screening rather than the whole service keeps cheap requests, such
    #: as browsing plants, from queueing behind an upload.
    PLANT_SCREENING_CONCURRENCY: int = 4

    # --- Badges ---
    #: Approved submissions needed to reach each growth stage of a
    #: contributor's tree, lowest first. Stage 0 is the bare ground before the
    #: first approval; reaching the last entry is the full-grown tree. The
    #: frontend draws one picture per stage, so the length of this list is the
    #: number of pictures it needs. Override as JSON, e.g. `[1,3,5,10,20]`.
    CONTRIBUTOR_TREE_STAGES: list[int] = [1, 3, 5, 10, 20]
    #: Decided submissions (approved or rejected) needed for each reviewer
    #: badge, lowest first; badge N is earned at the Nth threshold.
    #: **Placeholders** — the real tiers have not been decided yet. Change them
    #: here or through the environment; nothing is stored per user, so every
    #: account is re-graded against the new list at once.
    REVIEWER_BADGE_TIERS: list[int] = [10, 50, 100, 250]

    @field_validator("CONTRIBUTOR_TREE_STAGES", "REVIEWER_BADGE_TIERS")
    @classmethod
    def _strictly_increasing_counts(cls, value: list[int]) -> list[int]:
        """Refuse a ladder that cannot be climbed in order.

        A repeated or decreasing threshold would make two levels share a
        count or make a later one easier than an earlier one; failing at
        startup is better than serving badges in an order that makes no sense.
        """
        if not value:
            raise ValueError("needs at least one threshold")
        if value[0] < 1 or any(b <= a for a, b in zip(value, value[1:], strict=False)):
            raise ValueError(f"thresholds must be positive and strictly increasing, got {value}")
        return value

    # --- Email (optional) ---
    #: Resend API key. Without it the service still runs and logs what it
    #: would have sent, so a checkout with no credentials behaves like
    #: production minus the delivery.
    RESEND_API_KEY: str = ""
    #: Sender address. `onboarding@resend.dev` is Resend's shared test sender:
    #: no domain verification needed, but it only delivers to the address that
    #: owns the API key. Point this at a verified domain before anyone other
    #: than you is meant to receive mail.
    EMAIL_FROM: str = "Australian Poisonous Plants <onboarding@resend.dev>"
    #: Where a contributor's reply should go. Empty means no Reply-To header.
    EMAIL_REPLY_TO: str = ""

    @property
    def email_enabled(self) -> bool:
        """Whether outgoing mail can actually be delivered."""
        return bool(self.RESEND_API_KEY and self.EMAIL_FROM)

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
