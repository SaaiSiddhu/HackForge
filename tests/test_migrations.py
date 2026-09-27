import os
from pathlib import Path
from urllib.parse import urlparse
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect, text
from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_postgres_test_url() -> str:
    """Return dedicated test database URL, strictly isolated from production application database."""
    if os.environ.get("TEST_DATABASE_URL"):
        url = os.environ["TEST_DATABASE_URL"].strip()
    else:
        host = "db" if "@db:" in os.environ.get("DATABASE_URL", "") else "127.0.0.1"
        url = f"postgresql+psycopg://hackforge:hackforge-local-demo@{host}:5432/hackforge_test"

    validate_isolated_test_database(url)
    return url


def validate_isolated_test_database(url: str) -> None:
    """Strict isolation guard: Refuse to run against main application database 'hackforge'."""
    parsed = urlparse(url)
    db_name = parsed.path.lstrip("/")
    if db_name == "hackforge" or "test" not in db_name.lower():
        raise RuntimeError(
            f"REFUSING TO RUN MIGRATION TESTS against database '{db_name}'. "
            "Migration tests must run against an isolated test database (e.g. 'hackforge_test')."
        )


def is_postgres_available() -> bool:
    try:
        url = get_postgres_test_url()
        if not url.startswith("postgresql"):
            return False
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
def alembic_cfg():
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg


def test_sqlite_clean_migration_cycle(alembic_cfg, tmp_path, monkeypatch):
    """Verify clean migration on a temporary SQLite database (isolated schema check)."""
    db_file = tmp_path / "test_migration.db"
    db_url = f"sqlite:///{db_file}"
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("DATABASE_URL", db_url)

    from app.config import clear_settings_cache
    clear_settings_cache()

    try:
        # Run upgrade to head
        command.upgrade(alembic_cfg, "head")

        # Verify tables created
        engine = create_engine(db_url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        expected_tables = {
            "events", "tracks", "teams", "judges", "projects", "reviews",
            "accounts", "sessions", "event_roles", "event_questions",
            "event_audit_logs", "team_memberships", "team_invitations",
            "judge_tracks", "judge_invitations", "judge_assignments",
            "rubrics", "rubric_criteria", "review_criterion_scores",
            "alembic_version"
        }
        assert expected_tables.issubset(tables)

        # Second migration run should be completely harmless (idempotent)
        command.upgrade(alembic_cfg, "head")

        engine.dispose()
    finally:
        clear_settings_cache()


@pytest.mark.skipif(not is_postgres_available(), reason="PostgreSQL test database not available")
def test_postgres_migration_idempotence(alembic_cfg):
    """Verify running alembic upgrade head on isolated PostgreSQL test database is safe and idempotent."""
    test_db_url = get_postgres_test_url()
    alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)
    
    # Rerunning upgrade head should succeed cleanly without errors
    command.upgrade(alembic_cfg, "head")
    command.upgrade(alembic_cfg, "head")


def test_refuse_migration_against_application_database():
    """Verify isolation guard strictly refuses to run migrations against the application database."""
    prod_url = "postgresql+psycopg://hackforge:hackforge-local-demo@db:5432/hackforge"
    with pytest.raises(RuntimeError, match="REFUSING TO RUN MIGRATION TESTS"):
        validate_isolated_test_database(prod_url)
