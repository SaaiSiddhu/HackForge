import os
from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models import Project, Review


def get_postgres_url() -> str:
    """Return PostgreSQL connection URL from environment or Docker default."""
    if os.environ.get("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"]
    env_url = os.environ.get("DATABASE_URL", "")
    if env_url.startswith("postgresql"):
        return env_url
    return "postgresql+psycopg://hackforge:hackforge-local-demo@db:5432/hackforge"


def is_postgres_available() -> bool:
    """Check if PostgreSQL integration database is accessible."""
    url = get_postgres_url()
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not is_postgres_available(),
    reason="PostgreSQL database not available for integration tests",
)


@pytest.fixture(scope="module")
def pg_engine():
    url = get_postgres_url()
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


def test_postgres_foreign_key_enforcement(pg_engine):
    """Verify PostgreSQL enforces foreign key constraints on projects without committing."""
    with Session(pg_engine) as session:
        # Attempt to insert project referencing a nonexistent team
        invalid_project = Project(
            id="prj_fk_test_invalid",
            event_id="evt_01",
            team_id="tm_nonexistent_xyz",
            track_id="trk_01",
            title="FK Test Project",
            summary="Testing FK failure",
            repo_url="https://example.org/test",
            submitted_at=datetime.now(timezone.utc),
        )
        session.add(invalid_project)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_postgres_review_composite_pk_uniqueness(pg_engine):
    """Verify PostgreSQL enforces composite PK uniqueness (jdg_01, prj_07 already exists)."""
    with Session(pg_engine) as session:
        # Duplicate attempt with existing composite primary key ('jdg_01', 'prj_07') from fixtures
        duplicate_review = Review(
            judge_id="jdg_01",
            project_id="prj_07",
            criteria={"functionality": 5},
            comment="Duplicate review should fail unique constraint",
        )
        session.add(duplicate_review)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
