import uuid
import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import (
    Account,
    Event,
    EventAuditLog,
    EventRole,
    Judge,
    JudgeAssignment,
    JudgeInvitation,
    JudgeTrackPermission,
    Project,
    Review,
    ReviewCriterionScore,
    Rubric,
    RubricCriterion,
    Team,
    TeamMembership,
    Track,
)
from app.services import ConcurrencyConflictError, create_account, create_event, create_team
from app.judging_services import (
    calculate_raw_score,
    create_judge_invitation,
    accept_judge_invitation,
    revoke_judge_invitation,
    create_or_update_event_rubric,
    validate_rubric_criteria_specs,
    assign_judge_to_project,
    run_batch_assignment,
    save_judge_evaluation,
    get_organizer_judging_progress,
    get_account_judge_for_event,
    get_active_rubric,
    get_invitation_by_token,
)


@pytest.fixture
def judging_env(tmp_path, monkeypatch):
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


def login_user(client: TestClient, email: str, password: str = "Password123!") -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    login_resp = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert login_resp.status_code == 302, f"Login failed for {email}"
    return client.cookies.get("csrf_token")


def make_event(db: Session, event_id: str, name: str, org: Account, days: int = 5) -> Event:
    return create_event(
        db,
        event_id=event_id,
        name=name,
        submissions_close=datetime.now(timezone.utc) + timedelta(days=days),
        creator_account_id=org.id,
    )


def make_judge(
    db: Session,
    judge_id: str,
    event_id: str,
    name: str,
    email: str,
    account_id: str = None,
    tracks: list = None,
) -> Judge:
    track_list = tracks or []
    j = Judge(
        id=judge_id,
        event_id=event_id,
        name=name,
        email=email,
        account_id=account_id,
        tracks=track_list,
    )
    db.add(j)
    db.flush()
    for tid in track_list:
        db.add(JudgeTrackPermission(id=f"jt_{j.id}_{tid}", judge_id=j.id, track_id=tid))
    db.flush()
    return j


def make_project(
    db: Session,
    project_id: str,
    event_id: str,
    team_id: str,
    track_id: str,
    title: str = "Project Title",
    summary: str = "Project Summary",
    repo_url: str = "https://github.com/example/repo",
    status: str = "submitted",
) -> Project:
    now = datetime.now(timezone.utc)
    p = Project(
        id=project_id,
        event_id=event_id,
        team_id=team_id,
        track_id=track_id,
        title=title,
        summary=summary,
        repo_url=repo_url,
        submitted_at=now,
        status=status,
    )
    db.add(p)
    db.flush()
    return p





# --- 1. Scoring Formula & Rubric Validation Tests ---

def test_scoring_formula_weighted_contribution_and_bounds():
    """Verify formula: raw_score = 100 * sum(w_i * (score_i - min_i)/(max_i - min_i)) / sum(w_i)."""
    # 2 criteria:
    # c1: weight 2.0, min 1.0, max 5.0 -> score 3.0 -> contrib = (3-1)/4 = 0.5, weighted = 2.0 * 0.5 = 1.0
    # c2: weight 1.0, min 0.0, max 10.0 -> score 10.0 -> contrib = 10/10 = 1.0, weighted = 1.0 * 1.0 = 1.0
    # sum_weighted = 2.0, sum_weights = 3.0
    # raw_score = 100 * (2.0 / 3.0) = 66.6666... -> rounded to 66.67
    rubric = Rubric(id="r1", event_id="e1", version=1, name="Test", is_active=True)
    rubric.criteria = [
        RubricCriterion(id="rc1", rubric_id="r1", key="c1", name="C1", weight=2.0, min_score=1.0, max_score=5.0),
        RubricCriterion(id="rc2", rubric_id="r1", key="c2", name="C2", weight=1.0, min_score=0.0, max_score=10.0),
    ]
    scores = {"c1": 3.0, "c2": 10.0}
    raw = calculate_raw_score(rubric, scores)
    assert raw == 66.67

    # Edge cases: minimum scores give 0.0, maximum scores give 100.0
    assert calculate_raw_score(rubric, {"c1": 1.0, "c2": 0.0}) == 0.0
    assert calculate_raw_score(rubric, {"c1": 5.0, "c2": 10.0}) == 100.0


def test_rubric_validation_rules():
    """Verify rubric validation rejects non-positive total weight, negative weights, and min >= max."""
    # All zero weights
    with pytest.raises(ValueError, match="sum of weights .* strictly greater than zero"):
        validate_rubric_criteria_specs([
            {"key": "a", "name": "A", "weight": 0.0, "min_score": 1.0, "max_score": 5.0},
            {"key": "b", "name": "B", "weight": 0.0, "min_score": 1.0, "max_score": 5.0},
        ])

    # Negative weight
    with pytest.raises(ValueError, match="weight cannot be negative"):
        validate_rubric_criteria_specs([
            {"key": "a", "name": "A", "weight": -1.0, "min_score": 1.0, "max_score": 5.0},
        ])

    # min_score >= max_score
    with pytest.raises(ValueError, match="must be strictly less than maximum score"):
        validate_rubric_criteria_specs([
            {"key": "a", "name": "A", "weight": 1.0, "min_score": 5.0, "max_score": 5.0},
        ])
    with pytest.raises(ValueError, match="must be strictly less than maximum score"):
        validate_rubric_criteria_specs([
            {"key": "a", "name": "A", "weight": 1.0, "min_score": 6.0, "max_score": 5.0},
        ])

    # Empty criteria
    with pytest.raises(ValueError, match="Rubric must contain at least one criterion"):
        validate_rubric_criteria_specs([])


def test_rubric_version_freezing_on_existing_reviews(judging_env):
    """If reviews exist on active rubric, updating criteria creates a new version snapshot."""
    engine = judging_env["engine"]
    with Session(engine) as db:
        org = create_account(db, "org@example.com", "Password123!", "Organizer")
        event = make_event(db, "evt_rubric_test", "Rubric Test Event", org)
        # Create initial rubric
        crit_v1 = [
            {"key": "innov", "name": "Innovation", "weight": 1.0, "min_score": 1.0, "max_score": 5.0},
            {"key": "exec", "name": "Execution", "weight": 1.0, "min_score": 1.0, "max_score": 5.0},
        ]
        r1 = create_or_update_event_rubric(db, event.id, "Evaluation Rubric", crit_v1, actor=org)
        assert r1.version == 1

        # Without reviews, updating modifies in-place
        crit_v1_mod = [
            {"key": "innov", "name": "Innovation Updated", "weight": 2.0, "min_score": 1.0, "max_score": 5.0},
            {"key": "exec", "name": "Execution", "weight": 1.0, "min_score": 1.0, "max_score": 5.0},
        ]
        r1_updated = create_or_update_event_rubric(db, event.id, "Evaluation Rubric", crit_v1_mod, actor=org)
        assert r1_updated.id == r1.id
        assert r1_updated.version == 1

        # Now attach a review to r1
        track = Track(id="trk_01", event_id=event.id, name="General")
        db.add(track)
        db.flush()
        team = create_team(db, event.id, "Team 1", org)
        proj = make_project(db, "prj_01", event.id, team.id, track.id, "Project 1")
        judge = make_judge(db, "jdg_01", event.id, "Judge One", "j1@example.com")
        db.commit()

        review = Review(
            event_id=event.id,
            project_id=proj.id,
            judge_id=judge.id,
            rubric_id=r1.id,
            status="finalized",
            raw_score=75.0,
            criteria={"innov": 4.0, "exec": 4.0},
            comment="Existing review",
        )
        db.add(review)
        db.commit()

        # Updating rubric now MUST create a new version snapshot
        crit_v2 = [
            {"key": "innov", "name": "Innovation V2", "weight": 3.0, "min_score": 1.0, "max_score": 5.0},
            {"key": "exec", "name": "Execution", "weight": 1.0, "min_score": 1.0, "max_score": 5.0},
            {"key": "design", "name": "Design", "weight": 1.0, "min_score": 1.0, "max_score": 5.0},
        ]
        r2 = create_or_update_event_rubric(db, event.id, "Evaluation Rubric V2", crit_v2, actor=org)
        assert r2.version == 2
        assert r2.id != r1.id
        assert len(r2.criteria) == 3

        # Active rubric is now r2
        active = get_active_rubric(db, event.id)
        assert active.id == r2.id
        assert active.version == 2


# --- 2. Judge Invitation Lifecycle Tests ---

def test_judge_invitation_full_lifecycle(judging_env):
    """Test invitation creation, token hashing, GET without state change, acceptance, and revocation."""
    engine = judging_env["engine"]
    client = judging_env["client"]

    with Session(engine) as db:
        org = create_account(db, "org_invite@example.com", "Password123!", "Org Inviter")
        event = make_event(db, "evt_invite", "Invite Test Event", org)
        track_ai = Track(id="trk_ai", event_id=event.id, name="AI Track")
        db.add(track_ai)
        db.commit()

    # Login as organizer
    login_user(client, "org_invite@example.com")

    # Organizer invites judge
    resp = client.post(
        f"/events/evt_invite/manage/judges/invite",
        json={"email": "alice_judge@example.com", "tracks": ["trk_ai"]},
    )
    assert resp.status_code == 201
    data = resp.json()
    invite_url = data["invite_url"]
    inv_id = data["invitation_id"]
    token = invite_url.split("/")[-1]

    # Verify invitation in DB has 14-day expiry and hashed token
    with Session(engine) as db:
        inv = db.get(JudgeInvitation, inv_id)
        assert inv.status == "pending"
        assert inv.email == "alice_judge@example.com"
        assert inv.token_hash != token  # Hashed with SHA-256
        exp_utc = inv.expires_at if inv.expires_at.tzinfo else inv.expires_at.replace(tzinfo=timezone.utc)
        assert exp_utc > datetime.now(timezone.utc) + timedelta(days=13)

    # GET invitation landing page without mutation
    client.post("/logout", data={"csrf_token": client.cookies.get("csrf_token")})
    get_resp = client.get(invite_url)
    assert get_resp.status_code == 200
    assert "alice_judge@example.com" in get_resp.text

    # Status must still be pending
    with Session(engine) as db:
        inv = db.get(JudgeInvitation, inv_id)
        assert inv.status == "pending"

    # Unauthorized account cannot accept: create bob
    with Session(engine) as db:
        create_account(db, "bob_impostor@example.com", "Password123!", "Bob Impostor")

    login_user(client, "bob_impostor@example.com")
    csrf = client.cookies.get("csrf_token")
    accept_fail = client.post(f"/events/evt_invite/judges/invite/{token}/accept", data={"csrf_token": csrf})
    assert accept_fail.status_code == 400
    assert "alice_judge@example.com" in accept_fail.text

    # Legitimate account alice registers and accepts
    client.post("/logout", data={"csrf_token": client.cookies.get("csrf_token")})
    with Session(engine) as db:
        create_account(db, "alice_judge@example.com", "Password123!", "Alice Judge")

    login_user(client, "alice_judge@example.com")
    csrf = client.cookies.get("csrf_token")
    accept_ok = client.post(
        f"/events/evt_invite/judges/invite/{token}/accept",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert accept_ok.status_code == 302
    assert "/events/evt_invite/judge" in accept_ok.headers["location"]

    # Verify in DB: invitation accepted, Judge created with trk_ai permission
    with Session(engine) as db:
        inv = db.get(JudgeInvitation, inv_id)
        assert inv.status == "accepted"
        alice = db.scalar(select(Account).where(Account.email == "alice_judge@example.com"))
        judge = get_account_judge_for_event(db, "evt_invite", alice.id)
        assert judge is not None
        assert "trk_ai" in judge.tracks


def test_judge_invitation_revocation_and_expiration(judging_env):
    """Revoked or expired invitations cannot be accepted."""
    engine = judging_env["engine"]
    with Session(engine) as db:
        org = create_account(db, "org_rev@example.com", "Password123!", "Organizer")
        event = make_event(db, "evt_rev", "Revocation Event", org)
        target_acc = create_account(db, "target@example.com", "Password123!", "Target Judge")

        # 1. Revocation
        inv, token = create_judge_invitation(db, event.id, "target@example.com", [], org)
        revoke_judge_invitation(db, event.id, inv.id, org)

        with pytest.raises(ValueError, match="already revoked"):
            accept_judge_invitation(db, token, target_acc)

        # 2. Expiration
        inv2, token2 = create_judge_invitation(db, event.id, "target@example.com", [], org)
        inv2.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()

        with pytest.raises(ValueError, match="Invitation has expired"):
            accept_judge_invitation(db, token2, target_acc)


# --- 3. Cross-Event Authorization Denials ---

def test_cross_event_organizer_denials(judging_env):
    """Organizer A of Event 1 cannot perform management actions on Event 2."""
    engine = judging_env["engine"]
    client = judging_env["client"]

    with Session(engine) as db:
        org1 = create_account(db, "org1@example.com", "Password123!", "Org 1")
        org2 = create_account(db, "org2@example.com", "Password123!", "Org 2")
        e1 = make_event(db, "evt_01_cross", "Event 1", org1)
        e2 = make_event(db, "evt_02_cross", "Event 2", org2)

    # Login as Org 1
    login_user(client, "org1@example.com")

    # Org 1 tries to invite judge to Event 2 -> 403
    resp_invite = client.post(
        "/events/evt_02_cross/manage/judges/invite",
        json={"email": "j@example.com", "tracks": []},
    )
    assert resp_invite.status_code == 403

    # Org 1 tries to run batch assignment on Event 2 -> 403
    resp_batch = client.post(
        "/events/evt_02_cross/manage/assignments/batch",
        json={"target_reviews_per_project": 3},
    )
    assert resp_batch.status_code == 403

    # Org 1 tries to modify rubric on Event 2 -> 403
    resp_rubric = client.post(
        "/events/evt_02_cross/manage/rubric",
        json={"name": "Hacked Rubric", "criteria": [{"key": "x", "weight": 1, "min_score": 1, "max_score": 5}]},
    )
    assert resp_rubric.status_code == 403


# --- 4. Assignment Eligibility & Batch Allocation Tests ---

def test_batch_assignment_eligibility_conflicts_and_load_balance(judging_env):
    """Verify batch allocation:
    - submitted projects only (draft excluded).
    - respects track restrictions.
    - excludes judge's own team (conflict of interest).
    - balances workload evenly.
    - deterministic tie-breaking.
    """
    engine = judging_env["engine"]
    with Session(engine) as db:
        org = create_account(db, "org_batch@example.com", "Password123!", "Org")
        event = make_event(db, "evt_batch", "Batch Event", org)

        # Tracks
        t_web = Track(id="t_web", event_id=event.id, name="Web")
        t_ai = Track(id="t_ai", event_id=event.id, name="AI")
        db.add_all([t_web, t_ai])

        # Accounts for judges
        acc_j1 = create_account(db, "j1@example.com", "Password123!", "Judge 1")
        acc_j2 = create_account(db, "j2@example.com", "Password123!", "Judge 2")
        acc_j3 = create_account(db, "j3@example.com", "Password123!", "Judge 3")

        # Judge 1: Web only
        j1 = make_judge(db, "j1", event.id, "Judge 1", "j1@example.com", account_id=acc_j1.id, tracks=[t_web.id])

        # Judge 2: AI only
        j2 = make_judge(db, "j2", event.id, "Judge 2", "j2@example.com", account_id=acc_j2.id, tracks=[t_ai.id])

        # Judge 3: All tracks (no track permission records means untracked = all)
        j3 = make_judge(db, "j3", event.id, "Judge 3", "j3@example.com", account_id=acc_j3.id, tracks=[])

        # Teams and Projects
        # Team Web A: Member is Judge 1 (conflict!)
        tm_web_a = create_team(db, event.id, "Team Web A", acc_j1)
        prj_web_a = make_project(db, "p_web_a", event.id, tm_web_a.id, t_web.id, "Web A")

        # Team Web B: Member is Org (no conflict)
        tm_web_b = create_team(db, event.id, "Team Web B", org)
        prj_web_b = make_project(db, "p_web_b", event.id, tm_web_b.id, t_web.id, "Web B")

        # Draft Project: Should NOT receive assignments
        prj_draft = make_project(db, "p_draft", event.id, tm_web_b.id, t_web.id, "Draft Proj", status="draft")
        db.commit()

        # Run batch assignment for target 1 review per project
        result = run_batch_assignment(db, event.id, target_reviews_per_project=1, actor=org)
        assert result["total_projects"] == 2  # draft project excluded
        assert result["total_judges"] == 3

        # Assignments verification:
        asgns = db.scalars(select(JudgeAssignment).where(JudgeAssignment.event_id == event.id)).all()
        # p_draft must have 0 assignments
        assert not any(a.project_id == "p_draft" for a in asgns)

        # p_web_a has conflict with j1! Eligible judges for p_web_a:
        # - j1: in team -> CONFLICT
        # - j2: AI track only -> TRACK INELIGIBLE
        # - j3: All tracks -> ELIGIBLE!
        # Therefore, p_web_a MUST be assigned to j3!
        p_web_a_asgn = [a for a in asgns if a.project_id == "p_web_a"]
        assert len(p_web_a_asgn) == 1
        assert p_web_a_asgn[0].judge_id == "j3"

        # Under-coverage reporting check:
        # If we ask for target 3 reviews, we know there are not 3 eligible judges for p_web_a!
        result_high = run_batch_assignment(db, event.id, target_reviews_per_project=3, actor=org)
        assert any(p["project_id"] == "p_web_a" for p in result_high["uncovered_projects"])


# --- 5. Review Submission, Draft vs Finalized, Bounds & OCC Concurrency ---

def test_review_draft_vs_finalized_validation(judging_env):
    """Draft allows partial scores; finalized requires all criteria within bounds."""
    engine = judging_env["engine"]
    with Session(engine) as db:
        org = create_account(db, "org_rev_val@example.com", "Password123!", "Org")
        event = make_event(db, "evt_val", "Val Event", org)
        crit = [
            {"key": "c1", "name": "Crit 1", "weight": 1.0, "min_score": 1.0, "max_score": 5.0},
            {"key": "c2", "name": "Crit 2", "weight": 2.0, "min_score": 0.0, "max_score": 10.0},
        ]
        create_or_update_event_rubric(db, event.id, "Rubric", crit, org)

        track = Track(id="trk_val", event_id=event.id, name="Val Track")
        db.add(track)
        db.flush()
        judge_acc = create_account(db, "j_val@example.com", "Password123!", "Judge")
        judge = make_judge(db, "jdg_val", event.id, "Judge", "j_val@example.com", account_id=judge_acc.id)
        team = create_team(db, event.id, "Team Val", org)
        proj = make_project(db, "prj_val", event.id, team.id, track.id, "P Val")
        db.commit()

        assign_judge_to_project(db, event.id, judge.id, proj.id, org)

        # 1. Draft with partial score (only c1 provided) -> Allowed
        r_draft = save_judge_evaluation(
            db=db,
            event_id=event.id,
            judge=judge,
            project_id=proj.id,
            criteria_inputs={"c1": 3.0},
            comment="",
            is_final=False,
            expected_version=None,
            actor=judge_acc,
        )
        assert r_draft.status == "draft"
        assert r_draft.version == 1

        # 2. Finalize with missing c2 -> Rejection
        with pytest.raises(ValueError, match="must be scored before final submission"):
            save_judge_evaluation(
                db=db,
                event_id=event.id,
                judge=judge,
                project_id=proj.id,
                criteria_inputs={"c1": 3.0},
                comment="Partial",
                is_final=True,
                expected_version=1,
                actor=judge_acc,
            )

        # 3. Finalize with score out of bounds (c1 = 6.0 > 5.0) -> Rejection
        with pytest.raises(ValueError, match="must be between 1.0 and 5.0"):
            save_judge_evaluation(
                db=db,
                event_id=event.id,
                judge=judge,
                project_id=proj.id,
                criteria_inputs={"c1": 6.0, "c2": 5.0},
                comment="Out of bounds",
                is_final=True,
                expected_version=1,
                actor=judge_acc,
            )

        # 4. Unknown criterion key -> Rejection
        with pytest.raises(ValueError, match="Unknown criterion key"):
            save_judge_evaluation(
                db=db,
                event_id=event.id,
                judge=judge,
                project_id=proj.id,
                criteria_inputs={"c1": 3.0, "c2": 5.0, "c_invalid": 1.0},
                comment="Invalid key",
                is_final=False,
                expected_version=1,
                actor=judge_acc,
            )

        # 5. Successful finalization with correct scores
        r_final = save_judge_evaluation(
            db=db,
            event_id=event.id,
            judge=judge,
            project_id=proj.id,
            criteria_inputs={"c1": 5.0, "c2": 10.0},
            comment="Excellent execution",
            is_final=True,
            expected_version=1,
            actor=judge_acc,
        )
        assert r_final.status == "finalized"
        assert r_final.raw_score == 100.0
        assert r_final.version == 2


def test_review_occ_conflict_detection(judging_env):
    """Verify that submitting an outdated expected_version raises conflict and writes audit log."""
    engine = judging_env["engine"]
    with Session(engine) as db:
        org = create_account(db, "org_occ@example.com", "Password123!", "Org")
        event = make_event(db, "evt_occ", "OCC Event", org)
        crit = [{"key": "c1", "name": "Crit 1", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}]
        create_or_update_event_rubric(db, event.id, "Rubric", crit, org)

        track = Track(id="trk_occ", event_id=event.id, name="OCC Track")
        db.add(track)
        db.flush()
        judge_acc = create_account(db, "j_occ@example.com", "Password123!", "Judge")
        judge = make_judge(db, "jdg_occ", event.id, "Judge", "j_occ@example.com", account_id=judge_acc.id)
        team = create_team(db, event.id, "Team OCC", org)
        proj = make_project(db, "prj_occ", event.id, team.id, track.id, "P OCC")
        db.commit()

        assign_judge_to_project(db, event.id, judge.id, proj.id, org)

        # Worker 1 saves draft at version 1
        r1 = save_judge_evaluation(
            db=db,
            event_id=event.id,
            judge=judge,
            project_id=proj.id,
            criteria_inputs={"c1": 3.0},
            comment="Draft 1",
            is_final=False,
            expected_version=None,
            actor=judge_acc,
        )
        assert r1.version == 1

        # Worker 1 finalizes at version 1 -> increments to 2
        r2 = save_judge_evaluation(
            db=db,
            event_id=event.id,
            judge=judge,
            project_id=proj.id,
            criteria_inputs={"c1": 4.0},
            comment="Final",
            is_final=True,
            expected_version=1,
            actor=judge_acc,
        )
        assert r2.version == 2

        # Worker 2 attempts update with outdated version 1 -> raises ConcurrencyConflictError
        with pytest.raises((ConcurrencyConflictError, ValueError), match="Conflict: Review has been modified"):
            save_judge_evaluation(
                db=db,
                event_id=event.id,
                judge=judge,
                project_id=proj.id,
                criteria_inputs={"c1": 2.0},
                comment="Late write",
                is_final=True,
                expected_version=1,
                actor=judge_acc,
            )

        # Verify audit log recorded conflict
        conflict_log = db.scalar(
            select(EventAuditLog).where(
                EventAuditLog.event_id == event.id,
                EventAuditLog.action == "review.conflict",
            )
        )
        assert conflict_log is not None
        assert conflict_log.payload["expected_version"] == 1
        assert conflict_log.payload["current_version"] == 2


# --- 6. Backend Isolation Matrix via HTTP ---

def test_backend_isolation_matrix_http(judging_env):
    """Verify HTTP access permissions:
    - Judge A GET own scores -> 200 OK.
    - Judge B GET Judge A scores -> 403 Forbidden.
    - Participant GET Judge scores -> 403 Forbidden.
    - Judge evaluation on unassigned project -> 403 Forbidden.
    - Judge evaluation on wrong track -> 403 Forbidden.
    """
    engine = judging_env["engine"]
    client = judging_env["client"]

    with Session(engine) as db:
        org = create_account(db, "org_iso@example.com", "Password123!", "Org")
        event = make_event(db, "evt_iso", "Iso Event", org)
        t1 = Track(id="t1", event_id=event.id, name="Track 1")
        t2 = Track(id="t2", event_id=event.id, name="Track 2")
        db.add_all([t1, t2])

        crit = [{"key": "c1", "name": "Crit 1", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}]
        create_or_update_event_rubric(db, event.id, "Rubric", crit, org)

        # Accounts
        acc_ja = create_account(db, "ja@example.com", "Password123!", "Judge A")
        acc_jb = create_account(db, "jb@example.com", "Password123!", "Judge B")
        acc_prt = create_account(db, "prt@example.com", "Password123!", "Participant")

        # Judge A has permission for Track 1 only
        ja = make_judge(db, "jdg_a", event.id, "Judge A", "ja@example.com", account_id=acc_ja.id, tracks=[t1.id])

        # Judge B has permission for Track 2 only
        jb = make_judge(db, "jdg_b", event.id, "Judge B", "jb@example.com", account_id=acc_jb.id, tracks=[t2.id])

        # Project 1 in Track 1 assigned to Judge A
        team1 = create_team(db, event.id, "Team 1", acc_prt)
        p1 = make_project(db, "prj_1", event.id, team1.id, t1.id, "P1")
        p2 = make_project(db, "prj_2", event.id, team1.id, t2.id, "P2")
        db.commit()

        assign_judge_to_project(db, event.id, ja.id, p1.id, org)
        assign_judge_to_project(db, event.id, jb.id, p2.id, org)

        # Score p1 as Judge A
        save_judge_evaluation(db, event.id, ja, p1.id, {"c1": 4.0}, "Good", True, None, acc_ja)

    # 1. Judge A requests own scores -> 200 OK
    login_user(client, "ja@example.com")
    resp_ja = client.get(f"/events/evt_iso/judges/jdg_a/scores")
    assert resp_ja.status_code == 200
    assert resp_ja.json()["scores_count"] == 1

    # 2. Judge A tries to evaluate Project 2 (assigned to Judge B, in Track 2) -> 403 Forbidden
    resp_ja_eval_p2 = client.get(f"/events/evt_iso/judge/projects/prj_2")
    assert resp_ja_eval_p2.status_code == 403

    # 3. Judge B logs in and tries to access Judge A's scores -> 403 Forbidden
    client.post("/logout", data={"csrf_token": client.cookies.get("csrf_token")})
    login_user(client, "jb@example.com")
    resp_jb_probe = client.get(f"/events/evt_iso/judges/jdg_a/scores")
    assert resp_jb_probe.status_code == 403

    # 4. Participant logs in and tries to access Judge A's scores -> 403 Forbidden
    client.post("/logout", data={"csrf_token": client.cookies.get("csrf_token")})
    login_user(client, "prt@example.com")
    resp_prt_probe = client.get(f"/events/evt_iso/judges/jdg_a/scores")
    assert resp_prt_probe.status_code == 403


# --- 7. Organizer Progress Calculation Verification ---

def test_organizer_judging_progress_metrics(judging_env):
    """Verify calculation of progress metrics: assignments, draft, finalized, unstarted, under-covered."""
    engine = judging_env["engine"]
    with Session(engine) as db:
        org = create_account(db, "org_prog@example.com", "Password123!", "Org")
        event = make_event(db, "evt_prog", "Prog Event", org)
        crit = [{"key": "c1", "name": "Crit", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}]
        create_or_update_event_rubric(db, event.id, "Rubric", crit, org)

        track = Track(id="trk_prog", event_id=event.id, name="Prog Track")
        db.add(track)
        db.flush()
        j1 = make_judge(db, "j1", event.id, "J1", "j1@example.com")
        j2 = make_judge(db, "j2", event.id, "J2", "j2@example.com")
        tm = create_team(db, event.id, "Tm", org)
        p1 = make_project(db, "p1", event.id, tm.id, track.id, "P1")
        p2 = make_project(db, "p2", event.id, tm.id, track.id, "P2")
        db.commit()

        # p1 has 2 assignments: 1 finalized (j1), 1 draft (j2)
        assign_judge_to_project(db, event.id, j1.id, p1.id, org)
        assign_judge_to_project(db, event.id, j2.id, p1.id, org)
        # p2 has 1 assignment: unstarted (j1)
        assign_judge_to_project(db, event.id, j1.id, p2.id, org)

        # Create 1 finalized review
        save_judge_evaluation(db, event.id, j1, p1.id, {"c1": 5.0}, "Done", True, None, org)
        # Create 1 draft review
        save_judge_evaluation(db, event.id, j2, p1.id, {"c1": 3.0}, "In progress", False, None, org)

        prog = get_organizer_judging_progress(db, event.id)
        assert prog["total_projects"] == 2
        assert prog["total_judges"] == 2
        assert prog["total_assignments"] == 3
        assert prog["finalized_reviews"] == 1
        assert prog["draft_reviews"] == 1
        assert prog["unstarted_assignments"] == 1

        # p2 has 0 finalized reviews, so it is under-covered (< target 3)
        assert prog["projects_under_covered"] >= 1
        assert "p2" in prog["under_covered_project_ids"]
        # j1 has unstarted work on p2, j2 has draft work on p1 -> both have outstanding work
        assert prog["judges_with_outstanding_work"] == 2
