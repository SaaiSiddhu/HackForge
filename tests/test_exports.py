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
    assert sanitize_csv_cell(None) == ""
    assert sanitize_csv_cell("Clean Project Title") == "Clean Project Title"
    assert sanitize_csv_cell(42) == 42
    assert sanitize_csv_cell(99.5) == 99.5
    assert sanitize_csv_cell("-1.2345") == "-1.2345"
    assert sanitize_csv_cell(-0.85) == -0.85

    assert sanitize_csv_cell("=1+1") == "'=1+1"
    assert sanitize_csv_cell("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert sanitize_csv_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert sanitize_csv_cell("@SUM(A1:A10)") == "'@SUM(A1:A10)"
    assert sanitize_csv_cell("\tTabPrefixed") == "'\tTabPrefixed"
    assert sanitize_csv_cell("\rReturnPrefixed") == "'\rReturnPrefixed"
    assert sanitize_csv_cell("-2+3*cmd|' /C calc'!A0") == "'-2+3*cmd|' /C calc'!A0"


def test_full_authorization_matrix_results_snapshots_and_all_exports(exports_env):
    """Enforce strict authorization matrix across results pages, snapshot creation,
    snapshot viewing, all 6 CSV export routes, and snapshot-based exports.
    """
    engine = exports_env["engine"]
    client = exports_env["client"]

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        evt1 = Event(
            id="evt_exp_1",
            name="Event 1",
            submissions_open=now,
            submissions_close=now,
            registration_open=now,
            registration_close=now,
        )
        evt2 = Event(
            id="evt_exp_2",
            name="Event 2",
            submissions_open=now,
            submissions_close=now,
            registration_open=now,
            registration_close=now,
        )
        db.add_all([evt1, evt2])

        org_account = Account(id="acc_org_1", email="org@example.com", name="Org 1", password_hash="test_hash")
        cross_org_account = Account(
            id="acc_cross_org", email="cross@example.com", name="Cross Org", password_hash="test_hash"
        )
        part_account = Account(id="acc_part", email="part@example.com", name="Participant", password_hash="test_hash")
        judge_account = Account(
            id="acc_judge", email="judge@example.com", name="Judge Account", password_hash="test_hash"
        )
        dual_account = Account(id="acc_dual", email="dual@example.com", name="Dual Role", password_hash="test_hash")

        db.add_all([org_account, cross_org_account, part_account, judge_account, dual_account])
        db.flush()

        db.add(EventRole(id="er_1", event_id=evt1.id, account_id=org_account.id, role="organizer"))
        db.add(EventRole(id="er_2", event_id=evt2.id, account_id=cross_org_account.id, role="organizer"))
        db.add(EventRole(id="er_3", event_id=evt1.id, account_id=dual_account.id, role="organizer"))

        judge1 = Judge(
            id="jdg_pure",
            event_id=evt1.id,
            account_id=judge_account.id,
            email=judge_account.email,
            name="Pure Judge",
            tracks=["*"],
        )
        judge_dual = Judge(
            id="jdg_dual",
            event_id=evt1.id,
            account_id=dual_account.id,
            email=dual_account.email,
            name="Dual Judge",
            tracks=["*"],
        )
        rub1 = Rubric(id="rb_auth_1", event_id=evt1.id, name="Rubric 1", version=1, is_active=True)
        crit1 = RubricCriterion(
            id="rc_auth_1", rubric_id=rub1.id, key="c1", name="C1", weight=1.0, min_score=1.0, max_score=5.0, order_idx=0
        )
        db.add_all([judge1, judge_dual, rub1, crit1])

        make_auth_session(db, org_account.id, "tok_org_1")
        make_auth_session(db, cross_org_account.id, "tok_cross_org")
        make_auth_session(db, part_account.id, "tok_part")
        make_auth_session(db, judge_account.id, "tok_judge")
        make_auth_session(db, dual_account.id, "tok_dual")

        db.commit()

    # Create a snapshot as authorized organizer first so we can test snapshot viewing & snapshot exports
    snap_create_ok = client.post(
        "/events/evt_exp_1/manage/results/snapshots",
        json={"min_sample": 3, "epsilon": 1e-6},
        headers={"Authorization": "Bearer tok_org_1"},
    )
    assert snap_create_ok.status_code == 201
    snap_id = snap_create_ok.json()["snapshot_id"]

    get_routes = [
        "/events/evt_exp_1/manage/results",
        f"/events/evt_exp_1/manage/results?snapshot_id={snap_id}",
        "/events/evt_exp_1/export/judging.csv",
        "/events/evt_exp_1/export/reviews.csv",
        "/events/evt_exp_1/export/assignments.csv",
        "/events/evt_exp_1/export/teams.csv",
        "/events/evt_exp_1/export/projects.csv",
        "/events/evt_exp_1/export/judges-calibration.csv",
        f"/events/evt_exp_1/export/judging.csv?snapshot_id={snap_id}",
        f"/events/evt_exp_1/export/reviews.csv?snapshot_id={snap_id}",
        f"/events/evt_exp_1/export/judges-calibration.csv?snapshot_id={snap_id}",
    ]

    for route in get_routes:
        # 1. Unauthenticated visitor -> 401
        r_vis = client.get(route)
        assert r_vis.status_code == 401, f"Expected 401 for visitor on {route}, got {r_vis.status_code}"

        # 2. Participant -> 403
        r_part = client.get(route, headers={"Authorization": "Bearer tok_part"})
        assert r_part.status_code == 403, f"Expected 403 for participant on {route}"

        # 3. Cross-event organizer -> 403
        r_cross = client.get(route, headers={"Authorization": "Bearer tok_cross_org"})
        assert r_cross.status_code == 403, f"Expected 403 for cross-event organizer on {route}"

        # 4. Pure active judge -> 403
        r_judge = client.get(route, headers={"Authorization": "Bearer tok_judge"})
        assert r_judge.status_code == 403, f"Expected 403 for pure judge on {route}"

        # 5. Dual-role organizer who is active judge -> 403
        r_dual = client.get(route, headers={"Authorization": "Bearer tok_dual"})
        assert r_dual.status_code == 403, f"Expected 403 for dual-role judge/organizer on {route}"
        assert "Dual-role restriction" in r_dual.json().get("detail", "")

        # 6. Authorized non-judge organizer -> 200
        r_org = client.get(route, headers={"Authorization": "Bearer tok_org_1"})
        assert r_org.status_code == 200, f"Expected 200 for authorized organizer on {route}"

    # Snapshot creation POST authorization checks
    post_route = "/events/evt_exp_1/manage/results/snapshots"
    assert client.post(post_route, json={}).status_code == 401
    for tok in ("tok_part", "tok_cross_org", "tok_judge", "tok_dual"):
        r_post = client.post(post_route, json={}, headers={"Authorization": f"Bearer {tok}"})
        assert r_post.status_code == 403


def test_rfc4180_and_blanks_in_judging_export(exports_env):
    """Verify CSV export is strictly RFC 4180 compliant, properly quoted, and uses blanks for missing data."""
    engine = exports_env["engine"]
    client = exports_env["client"]

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        evt = Event(
            id="evt_csv",
            name="CSV Event",
            submissions_open=now,
            submissions_close=now,
            registration_open=now,
            registration_close=now,
            target_reviews_per_project=3,
        )
        org = Account(id="acc_csv_org", email="csv_org@example.com", name="CSV Org", password_hash="test_hash")
        db.add_all([evt, org])
        db.flush()
        db.add(EventRole(id="er_csv", event_id=evt.id, account_id=org.id, role="organizer"))

        rub = Rubric(id="rb_csv", event_id=evt.id, name="CSV Rubric", version=1, is_active=True)
        db.add(rub)
        crit = RubricCriterion(
            id="rc_csv", rubric_id=rub.id, key="c1", name="Crit 1", weight=1.0, min_score=0.0, max_score=10.0, order_idx=0
        )
        db.add(crit)

        team = Team(id="tm_csv", event_id=evt.id, name='Team "Comma, Quote" Name')
        db.add(team)

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

    reader = list(csv.reader(io.StringIO(content)))
    assert len(reader) >= 3  # Header + 2 projects

    header = reader[0]
    assert "rank" in header
    assert "project_id" in header
    assert "project_title" in header
    assert "normalized_index" in header
    assert "raw_mean" in header
    assert "component_id" in header
    assert "component_normalized_rank" in header

    p1_row = next(r for r in reader if len(r) > 1 and r[1] == "prj_csv_1")
    title_val = p1_row[2]
    assert title_val.startswith("'=cmd|calc")

    p2_row = next(r for r in reader if len(r) > 1 and r[1] == "prj_csv_2")
    norm_idx_val = p2_row[6]
    raw_mean_val = p2_row[8]
    assert norm_idx_val == ""
    assert raw_mean_val == ""


def test_snapshot_export_ownership_and_historical_immutability(exports_env):
    """Verify snapshot export derives event ownership from stored snapshot.event_id,
    rejecting cross-event leaks, and retains exact historical values after live reviews change.
    """
    engine = exports_env["engine"]
    client = exports_env["client"]

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        evt1 = Event(
            id="evt_snap_1",
            name="Event 1",
            submissions_open=now,
            submissions_close=now,
            registration_open=now,
            registration_close=now,
            target_reviews_per_project=1,
        )
        evt2 = Event(
            id="evt_snap_2",
            name="Event 2",
            submissions_open=now,
            submissions_close=now,
            registration_open=now,
            registration_close=now,
            target_reviews_per_project=1,
        )
        org1 = Account(id="acc_org_s1", email="org1@example.com", name="Org 1", password_hash="test_hash")
        org2 = Account(id="acc_org_s2", email="org2@example.com", name="Org 2", password_hash="test_hash")
        db.add_all([evt1, evt2, org1, org2])
        db.flush()
        db.add(EventRole(id="er_s1", event_id=evt1.id, account_id=org1.id, role="organizer"))
        db.add(EventRole(id="er_s2", event_id=evt2.id, account_id=org2.id, role="organizer"))

        trk1 = Track(id="trk_s1", event_id=evt1.id, name="Track S1")
        rub1 = Rubric(id="rb_s1", event_id=evt1.id, name="Rubric S1", version=1, is_active=True)
        crit1 = RubricCriterion(
            id="rc_s1", rubric_id=rub1.id, key="c1", name="C1", weight=1.0, min_score=0.0, max_score=100.0, order_idx=0
        )
        tm1 = Team(id="tm_s1", event_id=evt1.id, name="Team S1")
        proj1 = Project(
            id="prj_s1",
            event_id=evt1.id,
            team_id=tm1.id,
            track_id=trk1.id,
            title="Proj S1",
            summary="s",
            repo_url="r",
            submitted_at=now,
            status="submitted",
        )
        judge1 = Judge(id="jdg_s1", event_id=evt1.id, name="Judge S1", email="js1@e.com", tracks=["trk_s1"])
        rev1 = Review(
            judge_id=judge1.id,
            project_id=proj1.id,
            event_id=evt1.id,
            rubric_id=rub1.id,
            criteria={"c1": 60.0},
            comment="initial",
            status="finalized",
            raw_score=60.0,
        )
        rcs1 = ReviewCriterionScore(
            id="rcs_s1_1", judge_id=judge1.id, project_id=proj1.id, criterion_key="c1", score=60.0
        )
        db.add_all([trk1, rub1, crit1, tm1, proj1, judge1, rev1, rcs1])

        make_auth_session(db, org1.id, "tok_org_s1")
        make_auth_session(db, org2.id, "tok_org_s2")
        db.commit()

    # 1. Org 1 creates a snapshot on evt_snap_1 when score is 60.0
    snap_resp = client.post(
        "/events/evt_snap_1/manage/results/snapshots",
        json={"min_sample": 1, "epsilon": 1e-6},
        headers={"Authorization": "Bearer tok_org_s1"},
    )
    assert snap_resp.status_code == 201
    snap_id = snap_resp.json()["snapshot_id"]

    # Export snapshot CSVs before live mutation
    judging_snap_before = client.get(
        f"/events/evt_snap_1/export/judging.csv?snapshot_id={snap_id}",
        headers={"Authorization": "Bearer tok_org_s1"},
    )
    reviews_snap_before = client.get(
        f"/events/evt_snap_1/export/reviews.csv?snapshot_id={snap_id}",
        headers={"Authorization": "Bearer tok_org_s1"},
    )
    calib_snap_before = client.get(
        f"/events/evt_snap_1/export/judges-calibration.csv?snapshot_id={snap_id}",
        headers={"Authorization": "Bearer tok_org_s1"},
    )
    assert judging_snap_before.status_code == 200
    assert reviews_snap_before.status_code == 200
    assert calib_snap_before.status_code == 200

    # 2. Cross-event ownership verification:
    # Org 2 (authorized for evt_snap_2, NOT evt_snap_1) tries to view/export snap_id via evt_snap_2 URL -> 403!
    # Because _load_authorized_snapshot checks authorization on snapshot.event_id ("evt_snap_1") as well!
    for cross_url in (
        f"/events/evt_snap_2/manage/results?snapshot_id={snap_id}",
        f"/events/evt_snap_2/export/judging.csv?snapshot_id={snap_id}",
        f"/events/evt_snap_2/export/reviews.csv?snapshot_id={snap_id}",
        f"/events/evt_snap_2/export/judges-calibration.csv?snapshot_id={snap_id}",
    ):
        r_cross_own = client.get(cross_url, headers={"Authorization": "Bearer tok_org_s2"})
        assert r_cross_own.status_code in (403, 404)

    # Org 2 tries to access snap_id via evt_snap_1 URL -> 403
    r_direct = client.get(
        f"/events/evt_snap_1/export/judging.csv?snapshot_id={snap_id}",
        headers={"Authorization": "Bearer tok_org_s2"},
    )
    assert r_direct.status_code == 403

    # 3. Mutate live review score from 60.0 to 95.0
    with Session(engine) as db:
        rcs = db.get(ReviewCriterionScore, "rcs_s1_1")
        rev = db.get(Review, ("jdg_s1", "prj_s1"))
        rcs.score = 95.0
        rev.raw_score = 95.0
        rev.criteria = {"c1": 95.0}
        db.commit()

    # Live exports reflect 95.00
    live_judging = client.get(
        "/events/evt_snap_1/export/judging.csv",
        headers={"Authorization": "Bearer tok_org_s1"},
    ).text
    live_reviews = client.get(
        "/events/evt_snap_1/export/reviews.csv",
        headers={"Authorization": "Bearer tok_org_s1"},
    ).text
    assert "95.00" in live_judging
    assert "95.00" in live_reviews

    # Historical snapshot exports retain exact original values (60.00)
    judging_snap_after = client.get(
        f"/events/evt_snap_1/export/judging.csv?snapshot_id={snap_id}",
        headers={"Authorization": "Bearer tok_org_s1"},
    ).text
    reviews_snap_after = client.get(
        f"/events/evt_snap_1/export/reviews.csv?snapshot_id={snap_id}",
        headers={"Authorization": "Bearer tok_org_s1"},
    ).text
    calib_snap_after = client.get(
        f"/events/evt_snap_1/export/judges-calibration.csv?snapshot_id={snap_id}",
        headers={"Authorization": "Bearer tok_org_s1"},
    ).text

    assert judging_snap_after == judging_snap_before.text
    assert reviews_snap_after == reviews_snap_before.text
    assert calib_snap_after == calib_snap_before.text
    assert "60.00" in judging_snap_after
    assert "60.00" in reviews_snap_after
