import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class Settings(BaseModel):
    """Validated application settings for HackForge."""

    environment: Literal["development", "production", "testing"] = Field(
        default="development",
        description="Runtime environment: development, production, or testing",
    )
    database_url: str = Field(
        default="",
        description="Database connection URL (PostgreSQL required outside testing)",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Application logging level",
    )
    fixture_path: Path = Field(
        default=Path("fixtures.json"),
        description="Path to official fixtures JSON file",
    )
    seed_fixtures: bool = Field(
        default=True,
        description="Whether to run fixture seeding on startup",
    )
    host: str = Field(
        default="0.0.0.0",
        description="Host to bind HTTP server",
    )
    port: int = Field(
        default=8080,
        description="Port to bind HTTP server",
    )
    app_name: str = "HackForge"
    app_version: str = "0.1.0"

    @field_validator("seed_fixtures", mode="before")
    @classmethod
    def parse_seed_fixtures(cls, value):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "t")
        return bool(value)

    @field_validator("fixture_path", mode="before")
    @classmethod
    def parse_fixture_path(cls, value):
        if isinstance(value, str):
            return Path(value)
        return value

    @model_validator(mode="after")
    def validate_database_and_env(self) -> "Settings":
        # Disallow accidental SQLite fallback outside testing
        db_url = self.database_url.strip()
        if not db_url:
            if self.environment == "testing":
                db_url = "sqlite:///:memory:"
                self.database_url = db_url
            else:
                raise ValueError(
                    "DATABASE_URL must be specified for development and production environments. "
                    "Expected format: postgresql+psycopg://user:password@host:5432/dbname"
                )

        if db_url.startswith("sqlite") and self.environment != "testing":
            raise ValueError(
                "SQLite is strictly restricted to testing (ENVIRONMENT=testing). "
                "For development and production, use a PostgreSQL database URL."
            )

        return self


def load_settings_from_env() -> Settings:
    """Read configuration from environment variables and validate."""
    env = os.environ.get("ENVIRONMENT", os.environ.get("APP_ENV", "development"))
    db_url = os.environ.get("DATABASE_URL", "")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fixture_path = os.environ.get("FIXTURE_PATH", "fixtures.json")
    seed_fixtures = os.environ.get("SEED_FIXTURES", "true")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    return Settings(
        environment=env,
        database_url=db_url,
        log_level=log_level,
        fixture_path=fixture_path,
        seed_fixtures=seed_fixtures,
        host=host,
        port=port,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton application settings."""
    return load_settings_from_env()


def clear_settings_cache():
    """Clear cached settings for test isolation."""
    get_settings.cache_clear()
