import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models import (
    Account,
    Event,
    EventAuditLog,
    EventResultsSnapshot,
    Judge,
    JudgeAssignment,
    Project,
    Review,
    ReviewCriterionScore,
    Rubric,
    Team,
    Track,
)
from app.services import ConcurrencyConflictError, create_account, create_event, create_team
from app.judging_services import (
    assign_judge_to_project,
    create_or_update_event_rubric,
    save_judge_evaluation,
)

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

    # Deterministic coordination using threading.Barrier
    import threading

    barrier = threading.Barrier(2)

    # Two concurrent operations with expected_version = 1
    def run_worker(worker_id: int):
        with Session(pg_engine) as session:
            account = session.get(Account, user_id)
            # Coordinate so both workers issue their mutation attempt concurrently
            barrier.wait(timeout=5)
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

    winning_worker_result = r1 if r1[0] == "SUCCESS" else r2
    conflict_worker_result = r2 if r1[0] == "SUCCESS" else r1
    winning_title = winning_worker_result[2]

    # Verify final database state in hackforge_test
    with Session(pg_engine) as session:
        final_project = session.get(Project, project_id)
        assert final_project.version == 2
        # Prove winning content persists in the database
        assert final_project.title == winning_title
        assert final_project.title in ("Updated by Worker 1", "Updated by Worker 2")

        # Prove conflict error details
        assert "Conflict" in conflict_worker_result[1]

        # Exactly 1 project_updated audit record exists
        update_logs = session.scalars(
            select(EventAuditLog).where(
                EventAuditLog.event_id == event_id,
                EventAuditLog.action == "project_updated",
            )
        ).all()
        assert len(update_logs) == 1
        assert update_logs[0].payload["version"] == 2


def test_postgres_deadline_change_during_waiting_mutation(pg_engine):
    """Verify that a project mutation waiting on a row lock detects an organizer deadline change via explicit db.refresh."""
    import concurrent.futures
    import threading
    import time
    import uuid
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.models import Account, Event, Track
    from app.services import (
        create_account,
        create_event,
        create_team,
        save_project_draft_or_submission,
        update_event,
    )

    uid = uuid.uuid4().hex[:8]
    event_id = f"evt_dl_test_{uid}"

    # Setup open event and initial project at version 1
    with Session(pg_engine) as setup_session:
        user = create_account(
            setup_session,
            email=f"dl_test_{uid}@example.com",
            password="Password123!",
            name="DL Test Dev",
        )
        event = create_event(
            setup_session,
            event_id=event_id,
            name=f"DL Test Event {uid}",
            submissions_close=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        track = Track(id=f"trk_dl_{uid}", event_id=event.id, name="DL Track")
        setup_session.add(track)
        setup_session.commit()

        team = create_team(setup_session, event_id=event.id, name=f"DL Team {uid}", creator_account=user)
        p1 = save_project_draft_or_submission(
            db=setup_session,
            event_id=event.id,
            team_id=team.id,
            account=user,
            title="Initial DL Project",
            summary="Testing deadline change under lock",
            track_id=track.id,
        )
        assert p1.version == 1
        project_id = p1.id
        team_id = team.id
        user_id = user.id
        track_id = track.id

    lock_acquired_event = threading.Event()
    deadline_updated_event = threading.Event()

    def holder_worker():
        """Acquires lock on project row, waits for organizer to change deadline, then releases lock."""
        with Session(pg_engine) as holder_session:
            # Lock the project row
            stmt = select(Project).where(Project.id == project_id).with_for_update()
            p = holder_session.scalars(stmt).first()
            assert p is not None
            # Signal that lock is held
            lock_acquired_event.set()
            # Wait until deadline is updated to past
            deadline_updated_event.wait(timeout=10)
            time.sleep(0.1)  # Ensure update transaction has committed
            holder_session.commit()

    def mutation_worker():
        """Attempts to update project while lock is held. Will block, and when unblocked must see closed deadline."""
        # Wait until holder holds the project lock
        lock_acquired_event.wait(timeout=10)
        with Session(pg_engine) as mut_session:
            account = mut_session.get(Account, user_id)
            try:
                save_project_draft_or_submission(
                    db=mut_session,
                    event_id=event_id,
                    team_id=team_id,
                    account=account,
                    title="Attempted Mutation After Lock Wait",
                    summary="Should be rejected by refreshed deadline check",
                    track_id=track_id,
                    project_id=project_id,
                    expected_version=1,
                )
                return ("SUCCESS", None)
            except ValueError as ve:
                return ("REJECTED_DEADLINE", str(ve))
            except Exception as exc:
                return ("ERROR", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_holder = executor.submit(holder_worker)
        # Give holder thread a moment to acquire lock
        lock_acquired_event.wait(timeout=5)
        # Launch mutation worker (it will block on the row lock)
        f_mut = executor.submit(mutation_worker)
        time.sleep(0.1)

        # Organizer updates the event deadline to the past while mutation worker is waiting
        with Session(pg_engine) as org_session:
            past_deadline = datetime.now(timezone.utc) - timedelta(minutes=5)
            update_event(org_session, event_id=event_id, submissions_close=past_deadline)

        # Now signal holder worker to release the project lock
        deadline_updated_event.set()

        f_holder.result(timeout=10)
        mut_status, mut_msg = f_mut.result(timeout=10)

    assert mut_status == "REJECTED_DEADLINE", f"Expected REJECTED_DEADLINE, got: {mut_status}, msg: {mut_msg}"
    assert "closed on" in mut_msg

    # Verify project in DB was untouched
    with Session(pg_engine) as verify_session:
        p_check = verify_session.get(Project, project_id)
        assert p_check.version == 1
        assert p_check.title == "Initial DL Project"


def test_postgres_concurrent_review_creation_two_sessions(pg_engine):
    """Real PostgreSQL test: Two concurrent sessions attempt first review creation for same (judge, project).
    Exactly one must succeed, the other must receive ConcurrencyConflictError, and no partial criterion state exists.
    """
    import concurrent.futures
    import threading
    import uuid

    uid = uuid.uuid4().hex[:8]
    event_id = f"evt_rev_create_{uid}"

    with Session(pg_engine) as setup_session:
        org = create_account(setup_session, f"org_rev_cr_{uid}@example.com", "Password123!", "Org Cr")
        event = create_event(
            setup_session,
            event_id=event_id,
            name=f"Review Create {uid}",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=5),
            creator_account_id=org.id,
        )
        track = Track(id=f"trk_cr_{uid}", event_id=event.id, name="Track Cr")
        setup_session.add(track)
        setup_session.flush()

        crit = [{"key": "c1", "name": "Crit", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}]
        create_or_update_event_rubric(setup_session, event.id, "Rubric Cr", crit, org)

        judge_acc = create_account(setup_session, f"j_cr_{uid}@example.com", "Password123!", "Judge Cr")
        judge = Judge(
            id=f"jdg_cr_{uid}",
            event_id=event.id,
            name="Judge Cr",
            email=f"j_cr_{uid}@example.com",
            account_id=judge_acc.id,
            tracks=["*"],
        )
        setup_session.add(judge)
        setup_session.flush()

        team = create_team(setup_session, event.id, f"Team Cr {uid}", org)
        proj = Project(
            id=f"prj_cr_{uid}",
            event_id=event.id,
            team_id=team.id,
            track_id=track.id,
            title=f"Project Cr {uid}",
            summary="Project summary",
            repo_url="https://example.com/repo",
            status="submitted",
            submitted_at=datetime.now(timezone.utc),
        )
        setup_session.add(proj)
        setup_session.flush()

        assign_judge_to_project(setup_session, event.id, judge.id, proj.id, org)
        setup_session.commit()

        judge_id = judge.id
        project_id = proj.id
        judge_account_id = judge_acc.id

    barrier = threading.Barrier(2)

    def worker(worker_id: int):
        with Session(pg_engine) as session:
            j = session.get(Judge, judge_id)
            act = session.get(Account, judge_account_id)
            barrier.wait(timeout=5)
            try:
                r = save_judge_evaluation(
                    db=session,
                    event_id=event_id,
                    judge=j,
                    project_id=project_id,
                    criteria_inputs={"c1": float(worker_id + 1)},
                    comment=f"Worker {worker_id} review",
                    is_final=False,
                    expected_version=None,
                    actor=act,
                )
                session.commit()
                return ("SUCCESS", r.version, r.comment)
            except ConcurrencyConflictError as cce:
                session.rollback()
                return ("CONFLICT", str(cce))
            except Exception as exc:
                session.rollback()
                return ("ERROR", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker, 1)
        f2 = executor.submit(worker, 2)
        res1 = f1.result(timeout=10)
        res2 = f2.result(timeout=10)

    results = [res1, res2]
    statuses = [r[0] for r in results]
    assert "SUCCESS" in statuses, f"Expected one worker to succeed, got: {results}"
    assert "CONFLICT" in statuses, f"Expected one worker to get conflict, got: {results}"

    with Session(pg_engine) as verify_session:
        # Exactly one review exists in PostgreSQL
        reviews = verify_session.scalars(
            select(Review).where(Review.event_id == event_id, Review.judge_id == judge_id, Review.project_id == project_id)
        ).all()
        assert len(reviews) == 1
        assert reviews[0].version == 1

        # Criteria scores intact
        scores = verify_session.scalars(
            select(ReviewCriterionScore).where(
                ReviewCriterionScore.judge_id == judge_id,
                ReviewCriterionScore.project_id == project_id,
            )
        ).all()
        assert len(scores) == 1

        # Audit log has review_conflict entry
        conflict_log = verify_session.scalars(
            select(EventAuditLog).where(
                EventAuditLog.event_id == event_id,
                EventAuditLog.action == "review.conflict",
            )
        ).first()
        assert conflict_log is not None


def test_postgres_concurrent_review_edits_from_same_version(pg_engine):
    """Real PostgreSQL test: Two sessions read review at version 1, and concurrently submit edits with expected_version=1.
    Exactly one succeeds and advances to version 2; the other is rejected with ConcurrencyConflictError.
    """
    import concurrent.futures
    import threading
    import uuid

    uid = uuid.uuid4().hex[:8]
    event_id = f"evt_rev_edit_{uid}"

    with Session(pg_engine) as setup_session:
        org = create_account(setup_session, f"org_rev_ed_{uid}@example.com", "Password123!", "Org Ed")
        event = create_event(
            setup_session,
            event_id=event_id,
            name=f"Review Edit {uid}",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=5),
            creator_account_id=org.id,
        )
        track = Track(id=f"trk_ed_{uid}", event_id=event.id, name="Track Ed")
        setup_session.add(track)
        setup_session.flush()

        crit = [{"key": "c1", "name": "Crit", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}]
        create_or_update_event_rubric(setup_session, event.id, "Rubric Ed", crit, org)

        judge_acc = create_account(setup_session, f"j_ed_{uid}@example.com", "Password123!", "Judge Ed")
        judge = Judge(
            id=f"jdg_ed_{uid}",
            event_id=event.id,
            name="Judge Ed",
            email=f"j_ed_{uid}@example.com",
            account_id=judge_acc.id,
            tracks=["*"],
        )
        setup_session.add(judge)
        setup_session.flush()

        team = create_team(setup_session, event.id, f"Team Ed {uid}", org)
        proj = Project(
            id=f"prj_ed_{uid}",
            event_id=event.id,
            team_id=team.id,
            track_id=track.id,
            title=f"Project Ed {uid}",
            summary="Project summary",
            repo_url="https://example.com/repo",
            status="submitted",
            submitted_at=datetime.now(timezone.utc),
        )
        setup_session.add(proj)
        setup_session.flush()

        assign_judge_to_project(setup_session, event.id, judge.id, proj.id, org)

        # Initial review at version 1
        r_init = save_judge_evaluation(
            db=setup_session,
            event_id=event.id,
            judge=judge,
            project_id=proj.id,
            criteria_inputs={"c1": 2.0},
            comment="Initial v1",
            is_final=False,
            expected_version=None,
            actor=judge_acc,
        )
        assert r_init.version == 1
        setup_session.commit()

        judge_id = judge.id
        project_id = proj.id
        judge_account_id = judge_acc.id

    barrier = threading.Barrier(2)

    def edit_worker(worker_id: int):
        with Session(pg_engine) as session:
            j = session.get(Judge, judge_id)
            act = session.get(Account, judge_account_id)
            barrier.wait(timeout=5)
            try:
                r = save_judge_evaluation(
                    db=session,
                    event_id=event_id,
                    judge=j,
                    project_id=project_id,
                    criteria_inputs={"c1": 4.0},
                    comment=f"Worker {worker_id} edit",
                    is_final=False,
                    expected_version=1,
                    actor=act,
                )
                session.commit()
                return ("SUCCESS", r.version, r.comment)
            except ConcurrencyConflictError as cce:
                session.rollback()
                return ("CONFLICT", str(cce))
            except Exception as exc:
                session.rollback()
                return ("ERROR", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(edit_worker, 1)
        f2 = executor.submit(edit_worker, 2)
        res1 = f1.result(timeout=10)
        res2 = f2.result(timeout=10)

    results = [res1, res2]
    statuses = [r[0] for r in results]
    assert "SUCCESS" in statuses, f"Expected one edit to succeed: {results}"
    assert "CONFLICT" in statuses, f"Expected one edit to conflict: {results}"

    with Session(pg_engine) as verify_session:
        r_final = verify_session.scalars(
            select(Review).where(Review.judge_id == judge_id, Review.project_id == project_id)
        ).first()
        assert r_final.version == 2


def test_postgres_review_session_loaded_before_commit(pg_engine):
    """Real PostgreSQL test: Session B loads review into its session cache at version 1 before Session A commits version 2.
    When Session B attempts edit with expected_version=1, row lock and fresh refresh reveals version 2, raising ConcurrencyConflictError.
    """
    import uuid

    uid = uuid.uuid4().hex[:8]
    event_id = f"evt_rev_cache_{uid}"

    with Session(pg_engine) as setup_session:
        org = create_account(setup_session, f"org_rev_ca_{uid}@example.com", "Password123!", "Org Ca")
        event = create_event(
            setup_session,
            event_id=event_id,
            name=f"Review Cache {uid}",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=5),
            creator_account_id=org.id,
        )
        track = Track(id=f"trk_ca_{uid}", event_id=event.id, name="Track Ca")
        setup_session.add(track)
        setup_session.flush()

        crit = [{"key": "c1", "name": "Crit", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}]
        create_or_update_event_rubric(setup_session, event.id, "Rubric Ca", crit, org)

        judge_acc = create_account(setup_session, f"j_ca_{uid}@example.com", "Password123!", "Judge Ca")
        judge = Judge(
            id=f"jdg_ca_{uid}",
            event_id=event.id,
            name="Judge Ca",
            email=f"j_ca_{uid}@example.com",
            account_id=judge_acc.id,
            tracks=["*"],
        )
        setup_session.add(judge)
        setup_session.flush()

        team = create_team(setup_session, event.id, f"Team Ca {uid}", org)
        proj = Project(
            id=f"prj_ca_{uid}",
            event_id=event.id,
            team_id=team.id,
            track_id=track.id,
            title=f"Project Ca {uid}",
            summary="Project summary",
            repo_url="https://example.com/repo",
            status="submitted",
            submitted_at=datetime.now(timezone.utc),
        )
        setup_session.add(proj)
        setup_session.flush()

        assign_judge_to_project(setup_session, event.id, judge.id, proj.id, org)

        # Initial review at version 1
        r_init = save_judge_evaluation(
            db=setup_session,
            event_id=event.id,
            judge=judge,
            project_id=proj.id,
            criteria_inputs={"c1": 2.0},
            comment="Initial v1",
            is_final=False,
            expected_version=None,
            actor=judge_acc,
        )
        assert r_init.version == 1
        setup_session.commit()

        judge_id = judge.id
        project_id = proj.id
        judge_account_id = judge_acc.id

    # Step 1: Session B loads the review into its identity map (at version 1)
    session_b = Session(pg_engine)
    r_in_b = session_b.scalars(
        select(Review).where(Review.judge_id == judge_id, Review.project_id == project_id)
    ).first()
    assert r_in_b is not None
    assert r_in_b.version == 1

    # Step 2: Competing Session A updates review to version 2 and commits
    with Session(pg_engine) as session_a:
        j_a = session_a.get(Judge, judge_id)
        act_a = session_a.get(Account, judge_account_id)
        r_a = save_judge_evaluation(
            db=session_a,
            event_id=event_id,
            judge=j_a,
            project_id=project_id,
            criteria_inputs={"c1": 4.0},
            comment="Session A update to v2",
            is_final=False,
            expected_version=1,
            actor=act_a,
        )
        assert r_a.version == 2
        session_a.commit()

    # Step 3: Session B attempts to save using expected_version=1
    # Without populate_existing / refresh, session_b would see cached version 1.
    # With row lock and refresh, session_b detects that database state is version 2 and raises ConcurrencyConflictError!
    try:
        j_b = session_b.get(Judge, judge_id)
        act_b = session_b.get(Account, judge_account_id)
        with pytest.raises(ConcurrencyConflictError, match="Conflict: Review has been modified"):
            save_judge_evaluation(
                db=session_b,
                event_id=event_id,
                judge=j_b,
                project_id=project_id,
                criteria_inputs={"c1": 5.0},
                comment="Session B attempt with stale expectation",
                is_final=False,
                expected_version=1,
                actor=act_b,
            )
    finally:
        session_b.close()


def test_postgres_snapshot_consistent_read_and_reproduction_under_concurrent_review_change(pg_engine):
    """Real PostgreSQL test: While create_results_snapshot is in progress, a concurrent session
    modifies and commits review scores in PostgreSQL.
    Proves:
    1. Under REPEATABLE READ and single-pass canonical manifest capture, the snapshot's stored
       manifest, review hashes, and results_data all describe one consistent point-in-time state (t0).
    2. After the concurrent review changes are live in PostgreSQL (t1), reproduce_snapshot_results(snapshot)
       reproduces snapshot.results_data identically from the stored canonical manifest.
    3. check_snapshot_staleness(db, snapshot) detects the post-snapshot review updates.
    """
    import math
    import uuid
    from app.scoring_services import (
        calculate_event_results,
        check_snapshot_staleness,
        create_results_snapshot,
        reproduce_snapshot_results,
    )

    uid = uuid.uuid4().hex[:8]
    event_id = f"evt_snap_iso_{uid}"

    with Session(pg_engine) as setup_session:
        org = create_account(setup_session, f"org_snap_{uid}@example.com", "Password123!", "Org Snap")
        event = create_event(
            setup_session,
            event_id=event_id,
            name=f"Snapshot Isolation {uid}",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=5),
            creator_account_id=org.id,
        )
        track = Track(id=f"trk_snap_{uid}", event_id=event.id, name="Track Snap")
        setup_session.add(track)
        setup_session.flush()

        crit = [{"key": "c1", "name": "Crit", "weight": 1.0, "min_score": 0.0, "max_score": 100.0}]
        rub = create_or_update_event_rubric(setup_session, event.id, "Rubric Snap", crit, org)

        judge_acc = create_account(setup_session, f"j_snap_{uid}@example.com", "Password123!", "Judge Snap")
        judge = Judge(
            id=f"jdg_snap_{uid}",
            event_id=event.id,
            name="Judge Snap",
            email=f"j_snap_{uid}@example.com",
            account_id=judge_acc.id,
            tracks=[track.id],
        )
        setup_session.add(judge)
        setup_session.flush()

        initial_scores = [60.0, 80.0, 100.0]
        project_ids = []
        for idx, s_val in enumerate(initial_scores):
            tm = Team(id=f"tm_snap_{idx}_{uid}", event_id=event.id, name=f"Team Snap {idx} {uid}")
            setup_session.add(tm)
            setup_session.flush()
            p = Project(
                id=f"prj_snap_{idx}_{uid}",
                event_id=event.id,
                team_id=tm.id,
                track_id=track.id,
                title=f"Project Snap {idx}",
                summary="Summary",
                repo_url="https://example.com/repo",
                status="submitted",
                submitted_at=datetime.now(timezone.utc),
            )
            setup_session.add(p)
            setup_session.flush()
            project_ids.append(p.id)
            assign_judge_to_project(setup_session, event.id, judge.id, p.id, org)
            save_judge_evaluation(
                db=setup_session,
                event_id=event.id,
                judge=judge,
                project_id=p.id,
                criteria_inputs={"c1": s_val},
                comment=f"Initial score {s_val}",
                is_final=True,
                expected_version=None,
                actor=judge_acc,
            )

        setup_session.commit()
        org_id = org.id
        judge_id = judge.id
        judge_account_id = judge_acc.id
        p0_id, p1_id, p2_id = project_ids

    mutation_executed = {"done": False}

    def concurrent_writer_step():
        """Runs mid-snapshot after REPEATABLE READ transaction has started and frozen its MVCC snapshot at t0,
        but before Project, Review, and ReviewCriterionScore rows are queried by the snapshot reader.
        """
        with Session(pg_engine) as writer_session:
            j_w = writer_session.get(Judge, judge_id)
            act_w = writer_session.get(Account, judge_account_id)
            # Flip p0 score from 60 -> 100 and p2 score from 100 -> 20 in a separate committed transaction (t1)
            save_judge_evaluation(
                db=writer_session,
                event_id=event_id,
                judge=j_w,
                project_id=p0_id,
                criteria_inputs={"c1": 100.0},
                comment="Concurrent update p0 to 100",
                is_final=True,
                expected_version=1,
                actor=act_w,
            )
            save_judge_evaluation(
                db=writer_session,
                event_id=event_id,
                judge=j_w,
                project_id=p2_id,
                criteria_inputs={"c1": 20.0},
                comment="Concurrent update p2 to 20",
                is_final=True,
                expected_version=1,
                actor=act_w,
            )
            writer_session.commit()
            mutation_executed["done"] = True

    with Session(pg_engine) as snap_session:
        org_actor = snap_session.get(Account, org_id)
        snapshot = create_results_snapshot(
            db=snap_session,
            event_id=event_id,
            creator_account=org_actor,
            on_manifest_read_step=concurrent_writer_step,
        )
        snap_id = snapshot.id

    assert mutation_executed["done"] is True

    # Verify in a fresh session:
    with Session(pg_engine) as verify_session:
        loaded_snap = verify_session.get(EventResultsSnapshot, snap_id)
        assert loaded_snap is not None

        # 1. Snapshot's manifest and results_data both reflect the consistent t0 state (60, 80, 100)
        cohort_t0 = loaded_snap.results_data["cohorts"][0]
        p_t0_map = {p["project_id"]: p for p in cohort_t0["projects"]}
        assert math.isclose(p_t0_map[p0_id]["raw_mean"], 60.0, rel_tol=1e-9)
        assert math.isclose(p_t0_map[p1_id]["raw_mean"], 80.0, rel_tol=1e-9)
        assert math.isclose(p_t0_map[p2_id]["raw_mean"], 100.0, rel_tol=1e-9)
        assert p_t0_map[p2_id]["normalized_rank"] == 1
        assert p_t0_map[p0_id]["normalized_rank"] == 3

        manifest_revs = {
            r["project_id"]: r["criterion_scores"]["c1"]
            for r in loaded_snap.input_review_hashes["manifest"]["reviews"]
        }
        assert manifest_revs == {p0_id: 60.0, p1_id: 80.0, p2_id: 100.0}

        # 2. Live database results at t1 reflect the concurrent writer's commit (100, 80, 20)
        live_results = calculate_event_results(verify_session, event_id)
        p_live_map = {p["project_id"]: p for p in live_results["cohorts"][0]["projects"]}
        assert math.isclose(p_live_map[p0_id]["raw_mean"], 100.0, rel_tol=1e-9)
        assert math.isclose(p_live_map[p2_id]["raw_mean"], 20.0, rel_tol=1e-9)
        assert p_live_map[p0_id]["normalized_rank"] == 1

        # 3. Reproduce snapshot results purely from stored manifest -> matches t0 results_data identically!
        reproduced = reproduce_snapshot_results(loaded_snap)
        assert reproduced == loaded_snap.results_data

        # 4. Staleness check against live t1 database detects the 2 updated reviews
        staleness = check_snapshot_staleness(verify_session, loaded_snap)
        assert staleness["is_stale"] is True
        assert any("2 review(s) updated since snapshot" in r for r in staleness["stale_reasons"])


