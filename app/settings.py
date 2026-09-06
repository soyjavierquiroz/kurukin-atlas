"""Application configuration for Kurukin Atlas."""

from functools import lru_cache

from pydantic import Field, SecretStr
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
