import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse
from pydantic import BaseModel, Field, field_validator, model_validator


def parse_strict_bool(value: Any) -> bool:
    """Strictly parse boolean inputs, rejecting ambiguous or invalid values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (1, 1.0):
            return True
        if value in (0, 0.0):
            return False
        raise ValueError(f"Invalid boolean value: {value}")
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "t"):
            return True
        if v in ("false", "0", "no", "f"):
            return False
        raise ValueError(f"Invalid boolean string value: '{value}'")
    raise ValueError(f"Unsupported type for boolean parsing: {type(value)}")


def sanitize_url(url: str) -> str:
    """Mask credentials in database URLs for safe logging and error reporting."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username or ''}:***@{parsed.hostname or ''}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return url
    except Exception:
        return "<sanitized-url>"


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
    test_database_url: str = Field(
        default="",
        description="Dedicated test database URL for PostgreSQL integration tests",
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
    cookie_secure: bool = Field(
        default=False,
        description="Whether session cookies require HTTPS (Secure flag)",
    )
    upload_dir: Path = Field(
        default=Path("uploads"),
        description="Directory for locally stored image uploads",
    )
    enable_demo_provisioning: bool = Field(
        default=False,
        description="Gate for seeding demo accounts and sessions (strictly prohibited in production)",
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
    app_version: str = "0.2.0"

    @field_validator("seed_fixtures", mode="before")
    @classmethod
    def validate_seed_fixtures(cls, value: Any) -> bool:
        return parse_strict_bool(value)

    @field_validator("enable_demo_provisioning", mode="before")
    @classmethod
    def validate_enable_demo_provisioning(cls, value: Any) -> bool:
        return parse_strict_bool(value)

    @field_validator("cookie_secure", mode="before")
    @classmethod
    def validate_cookie_secure(cls, value: Any) -> bool:
        return parse_strict_bool(value)

    @field_validator("fixture_path", mode="before")
    @classmethod
    def parse_fixture_path(cls, value: Any) -> Path:
        if isinstance(value, str):
            return Path(value)
        return value

    @field_validator("upload_dir", mode="before")
    @classmethod
    def parse_upload_dir(cls, value: Any) -> Path:
        if isinstance(value, str):
            return Path(value)
        return value

    @model_validator(mode="after")
    def validate_database_and_env(self) -> "Settings":
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

        if self.environment != "testing":
            if db_url.startswith("sqlite"):
                raise ValueError(
                    "SQLite is strictly restricted to testing (ENVIRONMENT=testing). "
                    f"For {self.environment}, use a PostgreSQL database URL. Expected scheme: postgresql+psycopg://"
                )
            if not (db_url.startswith("postgresql+psycopg://") or db_url.startswith("postgresql://")):
                sanitized = sanitize_url(db_url)
                raise ValueError(
                    f"Invalid database scheme '{sanitized}'. PostgreSQL is required for {self.environment}. "
                    "Expected scheme: postgresql+psycopg://"
                )
            try:
                parsed = urlparse(db_url)
                if not parsed.hostname or not parsed.path or parsed.path == "/":
                    raise ValueError(f"Malformed PostgreSQL URL '{sanitize_url(db_url)}': missing host or database name.")
            except Exception as exc:
                if isinstance(exc, ValueError):
                    raise
                raise ValueError(f"Failed to parse DATABASE_URL: {exc}")
        else:
            # Testing environment allows sqlite or postgresql
            if not (db_url.startswith("sqlite") or db_url.startswith("postgresql")):
                raise ValueError(f"Invalid test database URL scheme: {sanitize_url(db_url)}")

        return self


def load_settings_from_env() -> Settings:
    """Read configuration from environment variables and validate."""
    env = os.environ.get("ENVIRONMENT", os.environ.get("APP_ENV", "development"))
    db_url = os.environ.get("DATABASE_URL", "")
    test_db_url = os.environ.get("TEST_DATABASE_URL", "")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fixture_path = os.environ.get("FIXTURE_PATH", "fixtures.json")
    seed_fixtures = os.environ.get("SEED_FIXTURES", "true")
    cookie_secure = os.environ.get("COOKIE_SECURE", "false")
    upload_dir = os.environ.get("UPLOAD_DIR", "uploads")
    enable_demo = os.environ.get("ENABLE_DEMO_PROVISIONING", "false")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    return Settings(
        environment=env,
        database_url=db_url,
        test_database_url=test_db_url,
        log_level=log_level,
        fixture_path=fixture_path,
        seed_fixtures=seed_fixtures,
        cookie_secure=cookie_secure,
        upload_dir=upload_dir,
        enable_demo_provisioning=enable_demo,
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
