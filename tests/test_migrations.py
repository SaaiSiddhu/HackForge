import os
from pathlib import Path
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect, text
from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_postgres_available() -> bool:
    settings = get_settings()
    url = os.environ.get("TEST_DATABASE_URL", settings.database_url)
    if not url.startswith("postgresql"):
        return False
    try:
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
        expected_tables = {"events", "tracks", "teams", "judges", "projects", "reviews", "alembic_version"}
        assert expected_tables.issubset(tables)

        # Second migration run should be completely harmless (idempotent)
        command.upgrade(alembic_cfg, "head")

        engine.dispose()
    finally:
        clear_settings_cache()


@pytest.mark.skipif(not is_postgres_available(), reason="PostgreSQL not available")
def test_postgres_migration_idempotence(alembic_cfg):
    """Verify running alembic upgrade head on PostgreSQL is safe and idempotent."""
    settings = get_settings()
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    # Rerunning upgrade head should succeed cleanly
    command.upgrade(alembic_cfg, "head")
