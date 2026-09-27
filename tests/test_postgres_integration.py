import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models import Event, Track, Team, Judge, Project, Review

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_postgres_test_url() -> str:
    """Return dedicated test database URL, strictly isolated from application database."""
    if os.environ.get("TEST_DATABASE_URL"):
        url = os.environ["TEST_DATABASE_URL"].strip()
    else:
        # Check inside docker container network vs localhost
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
            f"REFUSING TO RUN TESTS: Target database '{db_name}' is not isolated. "
            "Tests must run against 'hackforge_test' to avoid corrupting or destroying production/application data."
        )


def is_postgres_available() -> bool:
    """Check if PostgreSQL test database is accessible."""
    try:
        url = get_postgres_test_url()
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not is_postgres_available(),
    reason="PostgreSQL test database (hackforge_test) not available for integration tests",
)


@pytest.fixture(scope="module")
def pg_engine():
    """Provide isolated PostgreSQL engine with migrated schema on hackforge_test."""
    url = get_postgres_test_url()
    
    # Run migrations on hackforge_test
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


def test_refuse_run_against_application_database():
    """Verify isolation guard strictly refuses to run against the production database."""
    prod_url = "postgresql+psycopg://hackforge:hackforge-local-demo@db:5432/hackforge"
    with pytest.raises(RuntimeError, match="REFUSING TO RUN TESTS"):
        validate_isolated_test_database(prod_url)


def test_postgres_foreign_key_enforcement(pg_engine):
    """Verify PostgreSQL enforces foreign key constraints on projects without committing."""
    with Session(pg_engine) as session:
        # Seed an event and track for valid parent rows
        event = Event(id="evt_pg_test", name="PG Test Event", submissions_close=datetime.now(timezone.utc))
        track = Track(id="trk_pg_test", event_id="evt_pg_test", name="General")
        session.merge(event)
        session.merge(track)
        session.commit()

        # Attempt to insert project referencing a nonexistent team
        invalid_project = Project(
            id="prj_fk_test_invalid",
            event_id="evt_pg_test",
            team_id="tm_nonexistent_xyz",
            track_id="trk_pg_test",
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
    """Verify PostgreSQL enforces composite PK uniqueness (judge_id, project_id)."""
    with Session(pg_engine) as session:
        event = Event(id="evt_pg_test", name="PG Test Event", submissions_close=datetime.now(timezone.utc))
        track = Track(id="trk_pg_test", event_id="evt_pg_test", name="General")
        team = Team(id="tm_pg_test", event_id="evt_pg_test", name="Team Test", members=[{"name": "Alice"}])
        judge = Judge(id="jdg_pg_test", event_id="evt_pg_test", name="Judge Test", email="judge@test.local", tracks=["trk_pg_test"])
        project = Project(
            id="prj_pg_test",
            event_id="evt_pg_test",
            team_id="tm_pg_test",
            track_id="trk_pg_test",
            title="Composite PK Test Project",
            summary="Testing composite PK",
            repo_url="https://example.org/test",
            submitted_at=datetime.now(timezone.utc),
        )
        session.merge(event)
        session.merge(track)
        session.merge(team)
        session.merge(judge)
        session.merge(project)
        session.commit()

        rev1 = Review(
            judge_id="jdg_pg_test",
            project_id="prj_pg_test",
            criteria={"functionality": 5},
            comment="Initial review",
        )
        session.merge(rev1)
        session.commit()

        # Duplicate attempt with same composite primary key
        rev2 = Review(
            judge_id="jdg_pg_test",
            project_id="prj_pg_test",
            criteria={"functionality": 4},
            comment="Duplicate review should fail unique constraint",
        )
        session.add(rev2)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_postgres_concurrent_two_sessions_occ(pg_engine):
    """Verify true concurrent OCC conflict handling using two distinct PostgreSQL sessions on hackforge_test.

    Proves that when two concurrent transactions attempt to update the same project
    with the same expected_version=1:
    - One transaction succeeds, advancing version to 2.
    - The other transaction is blocked on row-level lock and subsequently raises ConcurrencyConflictError.
    - Exactly one update succeeds and exactly one version increment and audit record exist in the database.
    """
    import concurrent.futures
    import uuid
    from sqlalchemy import select
    from app.models import Account, EventAuditLog
    from app.services import (
        create_account,
        create_event,
        create_team,
        save_project_draft_or_submission,
        ConcurrencyConflictError,
    )

    uid = uuid.uuid4().hex[:8]
    event_id = f"evt_occ_pg_{uid}"

    # Setup seed data in initial transaction
    with Session(pg_engine) as setup_session:
        user = create_account(
            setup_session,
            email=f"occ_pg_{uid}@example.com",
            password="Password123!",
            name="OCC PG Dev",
        )
        event = create_event(
            setup_session,
            event_id=event_id,
            name=f"PG OCC Event {uid}",
            submissions_close=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        track = Track(id=f"trk_occ_pg_{uid}", event_id=event.id, name="PG OCC Track")
        setup_session.add(track)
        setup_session.commit()

        team = create_team(setup_session, event_id=event.id, name=f"PG OCC Team {uid}", creator_account=user)
        # Create initial project at version 1
        p1 = save_project_draft_or_submission(
            db=setup_session,
            event_id=event.id,
            team_id=team.id,
            account=user,
            title="Postgres OCC Initial",
            summary="Testing OCC on real Postgres",
            track_id=track.id,
        )
        assert p1.version == 1
        project_id = p1.id
        team_id = team.id
        user_id = user.id
        track_id = track.id

    # Verify initial audit logs
    with Session(pg_engine) as session:
        initial_logs = session.scalars(
            select(EventAuditLog).where(
                EventAuditLog.event_id == event_id,
                EventAuditLog.action == "project_created",
            )
        ).all()
        assert len(initial_logs) == 1

    # Two concurrent operations with expected_version = 1
    def run_worker(worker_id: int):
        with Session(pg_engine) as session:
            account = session.get(Account, user_id)
            try:
                res = save_project_draft_or_submission(
                    db=session,
                    event_id=event_id,
                    team_id=team_id,
                    account=account,
                    title=f"Updated by Worker {worker_id}",
                    summary=f"Worker {worker_id} update",
                    track_id=track_id,
                    project_id=project_id,
                    expected_version=1,
                )
                return ("SUCCESS", res.version, res.title)
            except ConcurrencyConflictError as cce:
                return ("CONFLICT", str(cce))
            except Exception as e:
                return ("ERROR", str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_worker, 1)
        f2 = executor.submit(run_worker, 2)
        r1 = f1.result(timeout=10)
        r2 = f2.result(timeout=10)

    results = [r1[0], r2[0]]
    assert results.count("SUCCESS") == 1, f"Expected exactly 1 SUCCESS, got: {results}"
    assert results.count("CONFLICT") == 1, f"Expected exactly 1 CONFLICT, got: {results}"

    # Verify final database state in hackforge_test
    with Session(pg_engine) as session:
        final_project = session.get(Project, project_id)
        assert final_project.version == 2

        # Exactly 1 project_updated audit record exists
        update_logs = session.scalars(
            select(EventAuditLog).where(
                EventAuditLog.event_id == event_id,
                EventAuditLog.action == "project_updated",
            )
        ).all()
        assert len(update_logs) == 1
        assert update_logs[0].payload["version"] == 2
