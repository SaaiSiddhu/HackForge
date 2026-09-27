import csv
import io
import pytest
from datetime import datetime, timezone, timedelta
from starlette.testclient import TestClient

from app.auth import hash_session_token
from app.config import get_settings
from app.database import Base, get_db
from app.export_services import sanitize_csv_cell
from app.main import app
from app.models import (
    Account,
    Event,
    EventRole,
    EventResultsSnapshot,
    Judge,
    JudgeAssignment,
    Project,
    Review,
    ReviewCriterionScore,
    Rubric,
    RubricCriterion,
    SessionModel,
    Team,
    Track,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def make_auth_session(db: Session, account_id: str, token: str) -> str:
    """Create a persistent session matching token for testing authentication."""
    now = datetime.now(timezone.utc)
    s = SessionModel(
        id=f"ses_{token}",
        account_id=account_id,
        token_hash=hash_session_token(token),
        expires_at=now + timedelta(days=7),
        created_at=now,
    )
    db.add(s)
    return token


@pytest.fixture
def exports_env(tmp_path, monkeypatch):
    """Provide isolated environment with in-memory DB and test client."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", upload_dir)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield {"engine": engine, "client": client}

    app.dependency_overrides.clear()
    engine.dispose()


def test_sanitize_csv_cell_formula_injection_and_blanks():
    """Verify formula injection defenses while preserving negative floats and blanks."""
    # Blank/None -> empty string
    assert sanitize_csv_cell(None) == ""

    # Safe strings
    assert sanitize_csv_cell("Clean Project Title") == "Clean Project Title"

    # Numeric types preserved
    assert sanitize_csv_cell(42) == 42
    assert sanitize_csv_cell(99.5) == 99.5

    # Safe negative numbers (represented as string or float)
    assert sanitize_csv_cell("-1.2345") == "-1.2345"
    assert sanitize_csv_cell(-0.85) == -0.85

    # Formula triggers: =, +, -, @, \t, \r
    assert sanitize_csv_cell("=1+1") == "'=1+1"
    assert sanitize_csv_cell("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert sanitize_csv_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert sanitize_csv_cell("@SUM(A1:A10)") == "'@SUM(A1:A10)"
    assert sanitize_csv_cell("\tTabPrefixed") == "'\tTabPrefixed"
    assert sanitize_csv_cell("\rReturnPrefixed") == "'\rReturnPrefixed"

    # Formula masquerading as negative number
    assert sanitize_csv_cell("-2+3*cmd|' /C calc'!A0") == "'-2+3*cmd|' /C calc'!A0"


def test_export_authorization_and_dual_role(exports_env):
    """Enforce strict authorization matrix on CSV export endpoints."""
    engine = exports_env["engine"]
    client = exports_env["client"]

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        # Event 1
        evt1 = Event(id="evt_exp_1", name="Event 1", submissions_open=now, submissions_close=now, registration_open=now, registration_close=now)
        # Event 2
        evt2 = Event(id="evt_exp_2", name="Event 2", submissions_open=now, submissions_close=now, registration_open=now, registration_close=now)
        db.add_all([evt1, evt2])

        # Accounts
        org_account = Account(id="acc_org_1", email="org@example.com", name="Org 1", password_hash="test_hash")
        cross_org_account = Account(id="acc_cross_org", email="cross@example.com", name="Cross Org", password_hash="test_hash")
        part_account = Account(id="acc_part", email="part@example.com", name="Participant", password_hash="test_hash")
        judge_account = Account(id="acc_judge", email="judge@example.com", name="Judge Account", password_hash="test_hash")
        dual_account = Account(id="acc_dual", email="dual@example.com", name="Dual Role", password_hash="test_hash")

        db.add_all([org_account, cross_org_account, part_account, judge_account, dual_account])
        db.flush()

        # Roles
        db.add(EventRole(id="er_1", event_id=evt1.id, account_id=org_account.id, role="organizer"))
        db.add(EventRole(id="er_2", event_id=evt2.id, account_id=cross_org_account.id, role="organizer"))
        db.add(EventRole(id="er_3", event_id=evt1.id, account_id=dual_account.id, role="organizer"))

        # Active judges
        judge1 = Judge(id="jdg_pure", event_id=evt1.id, account_id=judge_account.id, email=judge_account.email, name="Pure Judge", tracks=["*"])
        judge_dual = Judge(id="jdg_dual", event_id=evt1.id, account_id=dual_account.id, email=dual_account.email, name="Dual Judge", tracks=["*"])
        db.add_all([judge1, judge_dual])

        # Authentication sessions
        make_auth_session(db, org_account.id, "tok_org_1")
        make_auth_session(db, cross_org_account.id, "tok_cross_org")
        make_auth_session(db, part_account.id, "tok_part")
        make_auth_session(db, judge_account.id, "tok_judge")
        make_auth_session(db, dual_account.id, "tok_dual")

        db.commit()

    # 1. Unauthenticated -> 401
    res = client.get("/events/evt_exp_1/export/judging.csv")
    assert res.status_code == 401

    # 2. Participant -> 403
    res = client.get("/events/evt_exp_1/export/judging.csv", headers={"Authorization": "Bearer tok_part"})
    assert res.status_code == 403

    # 3. Cross-event organizer -> 403
    res = client.get("/events/evt_exp_1/export/judging.csv", headers={"Authorization": "Bearer tok_cross_org"})
    assert res.status_code == 403

    # 4. Pure active judge -> 403
    res = client.get("/events/evt_exp_1/export/judging.csv", headers={"Authorization": "Bearer tok_judge"})
    assert res.status_code == 403

    # 5. Dual-role organizer who is active judge -> 403
    res = client.get("/events/evt_exp_1/export/judging.csv", headers={"Authorization": "Bearer tok_dual"})
    assert res.status_code == 403
    assert "Dual-role restriction" in res.json().get("detail", "")

    # 6. Authorized organizer of evt_exp_1 -> 200
    res = client.get("/events/evt_exp_1/export/judging.csv", headers={"Authorization": "Bearer tok_org_1"})
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]


def test_rfc4180_and_blanks_in_judging_export(exports_env):
    """Verify CSV export is strictly RFC 4180 compliant, properly quoted, and uses blanks for missing data."""
    engine = exports_env["engine"]
    client = exports_env["client"]

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        evt = Event(id="evt_csv", name="CSV Event", submissions_open=now, submissions_close=now, registration_open=now, registration_close=now, target_reviews_per_project=3)
        org = Account(id="acc_csv_org", email="csv_org@example.com", name="CSV Org", password_hash="test_hash")
        db.add_all([evt, org])
        db.flush()
        db.add(EventRole(id="er_csv", event_id=evt.id, account_id=org.id, role="organizer"))

        rub = Rubric(id="rb_csv", event_id=evt.id, name="CSV Rubric", version=1, is_active=True)
        db.add(rub)
        crit = RubricCriterion(id="rc_csv", rubric_id=rub.id, key="c1", name="Crit 1", weight=1.0, min_score=0.0, max_score=10.0, order_idx=0)
        db.add(crit)

        team = Team(id="tm_csv", event_id=evt.id, name='Team "Comma, Quote" Name')
        db.add(team)

        # Project with quotes, comma, formula injection attempts in title
        proj1 = Project(
            id="prj_csv_1",
            event_id=evt.id,
            team_id=team.id,
            track_id="trk_csv",
            title='=cmd|calc!A0, "Dangerous Title"',
            tagline="Tagline with\r\nnewlines",
            summary="sum",
            repo_url="repo",
            submitted_at=now,
            status="submitted",
        )
        # Project 2 with no reviews
        proj2 = Project(
            id="prj_csv_2",
            event_id=evt.id,
            team_id=team.id,
            track_id="trk_csv",
            title="Clean Title",
            summary="sum",
            repo_url="repo",
            submitted_at=now,
            status="submitted",
        )
        db.add_all([proj1, proj2])
        make_auth_session(db, org.id, "tok_csv_org")
        db.commit()

    res = client.get("/events/evt_csv/export/judging.csv", headers={"Authorization": "Bearer tok_csv_org"})
    assert res.status_code == 200
    content = res.text

    # Verify RFC 4180 parsing via python csv module
    reader = list(csv.reader(io.StringIO(content)))
    assert len(reader) >= 3  # Header + 2 projects

    header = reader[0]
    assert "rank" in header
    assert "project_id" in header
    assert "project_title" in header
    assert "normalized_index" in header
    assert "raw_mean" in header

    # Project 1 row: Title must have formula injection escaped with single quote
    p1_row = next(r for r in reader if len(r) > 1 and r[1] == "prj_csv_1")
    title_val = p1_row[2]
    assert title_val.startswith("'=cmd|calc")

    # Project 2 row: unreviewed project must have blank values for scores, NOT zero!
    p2_row = next(r for r in reader if len(r) > 1 and r[1] == "prj_csv_2")
    norm_idx_val = p2_row[6]
    raw_mean_val = p2_row[8]
    assert norm_idx_val == ""  # Never fabricated "0" or "0.0"
    assert raw_mean_val == ""  # Never fabricated "0" or "0.0"


def test_snapshot_export_and_isolation(exports_env):
    """Verify snapshot export derives event ownership from snapshot, rejecting cross-event leaks."""
    engine = exports_env["engine"]
    client = exports_env["client"]

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        evt1 = Event(id="evt_snap_1", name="Event 1", submissions_open=now, submissions_close=now, registration_open=now, registration_close=now)
        evt2 = Event(id="evt_snap_2", name="Event 2", submissions_open=now, submissions_close=now, registration_open=now, registration_close=now)
        org1 = Account(id="acc_org_s1", email="org1@example.com", name="Org 1", password_hash="test_hash")
        org2 = Account(id="acc_org_s2", email="org2@example.com", name="Org 2", password_hash="test_hash")
        db.add_all([evt1, evt2, org1, org2])
        db.flush()
        db.add(EventRole(id="er_s1", event_id=evt1.id, account_id=org1.id, role="organizer"))
        db.add(EventRole(id="er_s2", event_id=evt2.id, account_id=org2.id, role="organizer"))

        snap1 = EventResultsSnapshot(
            id="snp_evt_snap_1_test",
            event_id=evt1.id,
            method="zscore_v1",
            parameters={},
            input_review_hashes={},
            results_data={"event_id": evt1.id, "cohorts": [{"cohort_id": "all:rb1", "projects": []}]},
            created_at=now,
        )
        db.add(snap1)
        make_auth_session(db, org1.id, "tok_org_s1")
        make_auth_session(db, org2.id, "tok_org_s2")
        db.commit()

    # 1. Org 1 exports snapshot from evt1 -> 200
    res = client.get(f"/events/evt_snap_1/export/judging.csv?snapshot_id=snp_evt_snap_1_test", headers={"Authorization": "Bearer tok_org_s1"})
    assert res.status_code == 200

    # 2. Org 2 tries to access snap1 via evt2 route -> 404 (mismatched event)
    res = client.get(f"/events/evt_snap_2/export/judging.csv?snapshot_id=snp_evt_snap_1_test", headers={"Authorization": "Bearer tok_org_s2"})
    assert res.status_code == 404

    # 3. Org 2 tries to access snap1 via evt1 route -> 403 (unauthorized for evt1)
    res = client.get(f"/events/evt_snap_1/export/judging.csv?snapshot_id=snp_evt_snap_1_test", headers={"Authorization": "Bearer tok_org_s2"})
    assert res.status_code == 403
