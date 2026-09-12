"""Application configuration for Kurukin Atlas."""

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    """Settings read from ``ATLAS_``-prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ATLAS_",
        extra="ignore",
    )

    env: str
    log_level: str

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: SecretStr = Field(repr=False)

    mbe_outbox: str
    source_cleanup_enabled: bool
    storage_backend: str
    storage_root: str
    rclone_binary: str = "rclone"
    rclone_remote: str | None = None
    rclone_root: str | None = None
    rclone_lock_path: str = "/opt/apps/kurukin-atlas/data/locks/rclone-drive.lock"

    @model_validator(mode="after")
    def validate_rclone_drive_settings(self) -> "Settings":
        """Keep local as the active default; validate Drive only when selected."""
        if self.storage_backend != "rclone_drive":
            return self
        if not self.rclone_binary.strip():
            raise ValueError("ATLAS_RCLONE_BINARY must be non-empty")
        if not self.rclone_remote or not self.rclone_remote.strip() or not self.rclone_remote.endswith(":"):
            raise ValueError("ATLAS_RCLONE_REMOTE must be a non-empty rclone remote ending in ':'")
        if not self.rclone_root or not self.rclone_root.strip():
            raise ValueError("ATLAS_RCLONE_ROOT must be non-empty")
        if not self.rclone_lock_path or not self.rclone_lock_path.strip():
            raise ValueError("ATLAS_RCLONE_LOCK_PATH must be non-empty")
        from app.storage.rclone_drive import _validate_root, _validate_remote
        _validate_remote(self.rclone_remote)
        _validate_root(self.rclone_root)
        return self

    @property
    def database_url(self) -> URL:
        """Return the database URL without manually assembling credentials."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
