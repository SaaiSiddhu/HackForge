import math
import statistics
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base
from app.models import (
    Account,
    Event,
    EventResultsSnapshot,
    Judge,
    JudgeAssignment,
    Project,
    Review,
    ReviewCriterionScore,
    Rubric,
    RubricCriterion,
    Team,
    TeamMembership,
    Track,
)
from app.scoring_services import (
    analyze_bipartite_connectivity,
    calculate_event_results,
    calculate_raw_review_score,
    check_snapshot_staleness,
    create_results_snapshot,
    evaluate_review_eligibility,
)


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """Provide isolated environment with in-memory DB session."""
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

    with Session(engine) as session:
        yield session

    engine.dispose()


def test_calculate_raw_review_score_unequal_weights_and_nonzero_min():
    """Verify raw review calculation matches formula:
    100 * sum(w_i * (s_i - min_i) / (max_i - min_i)) / sum(w_i)
    """
    class MockCrit:
        def __init__(self, key, weight, min_score, max_score):
            self.key = key
            self.weight = weight
            self.min_score = min_score
            self.max_score = max_score

    class MockRubric:
        def __init__(self, criteria):
            self.criteria = criteria

    # Criteria 1: weight 2.0, range 1 to 5, score 3 -> (3-1)/4 = 0.5. Weighted: 2.0 * 0.5 = 1.0
    # Criteria 2: weight 1.0, range 0 to 10, score 10 -> (10-0)/10 = 1.0. Weighted: 1.0 * 1.0 = 1.0
    # Criteria 3: weight 1.0, range 2 to 6, score 2 -> (2-2)/4 = 0.0. Weighted: 1.0 * 0.0 = 0.0
    # Sum weights = 4.0. Total weighted = 2.0. Expected raw = 100 * (2.0 / 4.0) = 50.0
    rubric = MockRubric([
        MockCrit("c1", 2.0, 1.0, 5.0),
        MockCrit("c2", 1.0, 0.0, 10.0),
        MockCrit("c3", 1.0, 2.0, 6.0),
    ])

    scores = {"c1": 3.0, "c2": 10.0, "c3": 2.0}
    raw = calculate_raw_review_score(rubric, scores)
    assert math.isclose(raw, 50.0, rel_tol=1e-9)


def test_calculate_raw_review_score_invalid_inputs():
    """Ensure invalid rubric or missing criteria raises clean ValueError."""
    class MockRubric:
        criteria = []

    with pytest.raises(ValueError, match="no criteria defined"):
        calculate_raw_review_score(MockRubric(), {"c1": 5.0})

    class MockCrit:
        key = "c1"
        weight = 0.0
        min_score = 1.0
        max_score = 5.0

    class ZeroWeightRubric:
        criteria = [MockCrit()]

    with pytest.raises(ValueError, match="strictly positive"):
        calculate_raw_review_score(ZeroWeightRubric(), {"c1": 5.0})


def test_evaluate_review_eligibility_policy():
    """Test the shared review eligibility policy across all edge conditions."""
    class MockReview:
        status = "finalized"

    class MockProject:
        status = "submitted"
        track_id = "trk_01"
        team_id = "team_01"

    class MockJudge:
        id = "jdg_01"
        account_id = "acc_judge"
        tracks = ["trk_01"]

    class MockCrit:
        key = "crit_1"
        weight = 1.0
        min_score = 1.0
        max_score = 5.0

    class MockRubric:
        criteria = [MockCrit()]

    # 1. Fully eligible
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members={"acc_other"},
    )
    assert eligible is True
    assert reason is None

    # 2. Draft review excluded
    draft_rev = MockReview()
    draft_rev.status = "draft"
    eligible, reason = evaluate_review_eligibility(
        review=draft_rev,
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members={"acc_other"},
    )
    assert eligible is False
    assert reason == "draft_review"

    # 3. Draft/withdrawn project excluded
    draft_proj = MockProject()
    draft_proj.status = "draft"
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=draft_proj,
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members={"acc_other"},
    )
    assert eligible is False
    assert reason == "project_status_draft"

    # 4. Conflict of interest: Judge on project's team
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members={"acc_judge", "acc_other"},
    )
    assert eligible is False
    assert reason == "conflict_of_interest"

    # 5. Track mismatch
    unauthorized_judge = MockJudge()
    unauthorized_judge.tracks = ["trk_02"]
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=unauthorized_judge,
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members=set(),
    )
    assert eligible is False
    assert reason == "track_mismatch"

    # 6. Incomplete criteria score
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={},
        team_members=set(),
    )
    assert eligible is False
    assert reason == "missing_criterion_crit_1"

    # 7. Out of bounds criteria score
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 6.0},  # max is 5.0
        team_members=set(),
    )
    assert eligible is False
    assert reason == "out_of_bounds_criterion_crit_1"


def test_bipartite_connectivity_analysis():
    """Verify connected components detection and warning logic."""
    # Graph 1: Fully connected
    # Judge 1: P1, P2
    # Judge 2: P2, P3
    res1 = analyze_bipartite_connectivity(
        project_ids=["p1", "p2", "p3"],
        usable_judge_evals=[("j1", "p1"), ("j1", "p2"), ("j2", "p2"), ("j2", "p3")],
    )
    assert res1["is_connected"] is True
    assert res1["component_count"] == 1
    assert res1["warning"] is None

    # Graph 2: Disconnected into two components
    # Component A: J1 evaluates P1, P2
    # Component B: J2 evaluates P3, P4
    res2 = analyze_bipartite_connectivity(
        project_ids=["p1", "p2", "p3", "p4"],
        usable_judge_evals=[("j1", "p1"), ("j1", "p2"), ("j2", "p3"), ("j2", "p4")],
    )
    assert res2["is_connected"] is False
    assert res2["component_count"] == 2
    assert "partitioned into 2 disconnected components" in res2["warning"]
    assert res2["project_component_map"]["p1"] == "Component 1"
    assert res2["project_component_map"]["p3"] == "Component 2"


def test_population_standard_deviation_and_zscore(db_session):
    """Test population standard deviation against known values and statistics.pstdev."""
    # Create test event with 1 judge and 3 projects
    now = datetime.now(timezone.utc)
    evt = Event(
        id="evt_test_math",
        name="Math Test Event",
        submissions_open=now,
        submissions_close=now,
        registration_open=now,
        registration_close=now,
        target_reviews_per_project=2,
    )
    db_session.add(evt)

    rub = Rubric(id="rb_test_math", event_id=evt.id, name="Test Rubric", version=1, is_active=True)
    db_session.add(rub)
    crit = RubricCriterion(id="rc_test_m", rubric_id=rub.id, key="score", name="Score", weight=1.0, min_score=0.0, max_score=100.0, order_idx=0)
    db_session.add(crit)

    judge = Judge(id="jdg_test_math", event_id=evt.id, name="Math Judge", email="math@example.com", tracks=["*"])
    db_session.add(judge)

    # Scores: 60, 80, 100
    # Mean: 80
    # Deviations: -20, 0, 20. Squared: 400, 0, 400. Sum = 800.
    # Population Variance = 800 / 3 = 266.6667
    # Population StdDev = sqrt(800/3) = 16.3299316...
    scores = [60.0, 80.0, 100.0]
    expected_pstdev = statistics.pstdev(scores)

    for i, s in enumerate(scores):
        team = Team(id=f"tm_math_{i}", event_id=evt.id, name=f"Team {i}")
        db_session.add(team)
        proj = Project(id=f"prj_math_{i}", event_id=evt.id, team_id=team.id, track_id="trk_math", title=f"Proj {i}", summary="s", repo_url="r", submitted_at=now, status="submitted")
        db_session.add(proj)
        rev = Review(judge_id=judge.id, project_id=proj.id, event_id=evt.id, rubric_id=rub.id, criteria={"score": s}, comment="good", status="finalized", raw_score=s)
        db_session.add(rev)
        rc = ReviewCriterionScore(id=f"rcs_m_{i}", judge_id=judge.id, project_id=proj.id, criterion_key="score", score=s)
        db_session.add(rc)

    db_session.commit()

    results = calculate_event_results(db_session, evt.id, min_sample=3, epsilon=1e-6)
    cohort = results["cohorts"][0]
    j_stat = next(j for j in cohort["judges"] if j["judge_id"] == judge.id)

    assert j_stat["sample_count_N"] == 3
    assert math.isclose(j_stat["mean_score"], 80.0, rel_tol=1e-6)
    assert math.isclose(j_stat["population_std_dev"], expected_pstdev, rel_tol=1e-6)
    assert j_stat["is_usable"] is True

    # Check project z-scores
    # Proj 0: (60 - 80) / sigma = -20 / 16.32993 = -1.2247448...
    # Proj 1: (80 - 80) / sigma = 0
    # Proj 2: (100 - 80) / sigma = +1.2247448...
    p0 = next(p for p in cohort["projects"] if p["project_id"] == "prj_math_0")
    p1 = next(p for p in cohort["projects"] if p["project_id"] == "prj_math_1")
    p2 = next(p for p in cohort["projects"] if p["project_id"] == "prj_math_2")

    assert math.isclose(p0["normalized_index"], -20.0 / expected_pstdev, rel_tol=1e-6)
    assert math.isclose(p1["normalized_index"], 0.0, abs_tol=1e-6)
    assert math.isclose(p2["normalized_index"], 20.0 / expected_pstdev, rel_tol=1e-6)


def test_near_zero_variance_and_small_sample_guards(db_session):
    """Verify that judges with N < 3 or near-zero variance are excluded from normalization."""
    now = datetime.now(timezone.utc)
    evt = Event(id="evt_guards", name="Guards Event", submissions_open=now, submissions_close=now, registration_open=now, registration_close=now, target_reviews_per_project=3)
    db_session.add(evt)
    rub = Rubric(id="rb_guards", event_id=evt.id, name="R", version=1, is_active=True)
    db_session.add(rub)
    crit = RubricCriterion(id="rc_guards", rubric_id=rub.id, key="s", name="S", weight=1.0, min_score=0.0, max_score=10.0, order_idx=0)
    db_session.add(crit)

    # Judge 1: N=2 (< 3)
    j_small = Judge(id="jdg_small", event_id=evt.id, name="Small N", email="s@e.com", tracks=["*"])
    # Judge 2: Constant scores (std_dev = 0)
    j_zero = Judge(id="jdg_zero", event_id=evt.id, name="Zero Var", email="z@e.com", tracks=["*"])
    db_session.add_all([j_small, j_zero])

    team = Team(id="tm_g1", event_id=evt.id, name="Team G")
    db_session.add(team)
    for i in range(4):
        p = Project(id=f"prj_g_{i}", event_id=evt.id, team_id=team.id, track_id="trk_g", title=f"P {i}", summary="s", repo_url="r", submitted_at=now, status="submitted")
        db_session.add(p)

    # Add 2 reviews for j_small (different scores)
    for i, s in enumerate([5.0, 8.0]):
        db_session.add(Review(judge_id=j_small.id, project_id=f"prj_g_{i}", event_id=evt.id, rubric_id=rub.id, criteria={"s": s}, comment="", status="finalized", raw_score=s*10))
        db_session.add(ReviewCriterionScore(id=f"rcs_s_{i}", judge_id=j_small.id, project_id=f"prj_g_{i}", criterion_key="s", score=s))

    # Add 4 reviews for j_zero with IDENTICAL score (7.0)
    for i in range(4):
        db_session.add(Review(judge_id=j_zero.id, project_id=f"prj_g_{i}", event_id=evt.id, rubric_id=rub.id, criteria={"s": 7.0}, comment="", status="finalized", raw_score=70.0))
        db_session.add(ReviewCriterionScore(id=f"rcs_z_{i}", judge_id=j_zero.id, project_id=f"prj_g_{i}", criterion_key="s", score=7.0))

    db_session.commit()

    results = calculate_event_results(db_session, evt.id, min_sample=3, epsilon=1e-6)
    cohort = results["cohorts"][0]

    j_small_stat = next(j for j in cohort["judges"] if j["judge_id"] == j_small.id)
    assert j_small_stat["is_usable"] is False
    assert "insufficient_samples" in j_small_stat["unusable_reason"]

    j_zero_stat = next(j for j in cohort["judges"] if j["judge_id"] == j_zero.id)
    assert j_zero_stat["is_usable"] is False
    assert "near_zero_variance" in j_zero_stat["unusable_reason"]

    # All projects have 0 usable normalized scores, so normalized_index is None and provisional is True
    for p in cohort["projects"]:
        assert p["normalized_index"] is None
        assert p["is_provisional"] is True
        assert p["raw_mean"] is not None  # Raw scores are retained!


def test_snapshot_lifecycle_and_staleness(db_session):
    """Test snapshot creation, immutability, and staleness detection upon review changes."""
    now = datetime.now(timezone.utc)
    evt = Event(id="evt_snap", name="Snap Event", submissions_open=now, submissions_close=now, registration_open=now, registration_close=now, target_reviews_per_project=1)
    db_session.add(evt)
    rub = Rubric(id="rb_snap", event_id=evt.id, name="R", version=1, is_active=True)
    db_session.add(rub)
    crit = RubricCriterion(id="rc_snap", rubric_id=rub.id, key="s", name="S", weight=1.0, min_score=0.0, max_score=10.0, order_idx=0)
    db_session.add(crit)

    judge = Judge(id="jdg_snap", event_id=evt.id, name="Judge", email="j@e.com", tracks=["*"])
    team = Team(id="tm_snap", event_id=evt.id, name="Team")
    proj = Project(id="prj_snap", event_id=evt.id, team_id=team.id, track_id="trk_s", title="P", summary="s", repo_url="r", submitted_at=now, status="submitted")
    db_session.add_all([judge, team, proj])

    rev = Review(judge_id=judge.id, project_id=proj.id, event_id=evt.id, rubric_id=rub.id, criteria={"s": 5.0}, comment="ok", status="finalized", raw_score=50.0)
    rc = ReviewCriterionScore(id="rcs_snap", judge_id=judge.id, project_id=proj.id, criterion_key="s", score=5.0)
    db_session.add_all([rev, rc])
    db_session.commit()

    # 1. Create snapshot
    snapshot = create_results_snapshot(db_session, evt.id, creator_account=None)
    assert snapshot.id.startswith("snp_")
    
    # Check freshness
    freshness = check_snapshot_staleness(db_session, snapshot)
    assert freshness["is_stale"] is False
    assert freshness["stale_reasons"] == []

    # 2. Modify review criterion score in live DB
    rc.score = 9.0
    rev.updated_at = datetime.now(timezone.utc)
    db_session.commit()

    # Check staleness
    staleness = check_snapshot_staleness(db_session, snapshot)
    assert staleness["is_stale"] is True
    assert any("updated since snapshot" in r for r in staleness["stale_reasons"])
