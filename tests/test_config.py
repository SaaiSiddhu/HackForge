import pytest
from pydantic import ValidationError
from app.config import Settings


def test_settings_valid_defaults():
    s = Settings(
        environment="testing",
        database_url="sqlite:///:memory:",
    )
    assert s.environment == "testing"
    assert s.database_url == "sqlite:///:memory:"
    assert s.seed_fixtures is True
    assert s.log_level == "INFO"


def test_settings_rejects_sqlite_in_production():
    with pytest.raises(ValidationError, match="SQLite is strictly restricted to testing"):
        Settings(
            environment="production",
            database_url="sqlite:///hackforge.db",
        )


def test_settings_rejects_sqlite_in_development():
    with pytest.raises(ValidationError, match="SQLite is strictly restricted to testing"):
        Settings(
            environment="development",
            database_url="sqlite:///hackforge.db",
        )


def test_settings_requires_database_url_in_production():
    with pytest.raises(ValidationError, match="DATABASE_URL must be specified"):
        Settings(
            environment="production",
            database_url="",
        )


def test_settings_parses_boolean_seed_fixtures():
    s1 = Settings(environment="testing", seed_fixtures="false")
    assert s1.seed_fixtures is False
    s2 = Settings(environment="testing", seed_fixtures="1")
    assert s2.seed_fixtures is True
