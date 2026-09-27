import csv
import io
import math
import statistics
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base
from app.export_services import generate_judging_results_csv, generate_reviews_csv
from app.judging_services import calculate_raw_score
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
    reproduce_snapshot_results,
    validate_normalization_parameters,
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

    rubric = MockRubric([
        MockCrit("c1", 2.0, 1.0, 5.0),
        MockCrit("c2", 1.0, 0.0, 10.0),
        MockCrit("c3", 1.0, 2.0, 6.0),
    ])

    scores = {"c1": 3.0, "c2": 10.0, "c3": 2.0}
    raw = calculate_raw_review_score(rubric, scores)
    assert math.isclose(raw, 50.0, rel_tol=1e-9)


def test_calculate_raw_review_score_and_parameters_reject_invalid_inputs():
    """Ensure zero/negative ranges, negative/non-finite weights, NaN/inf, out-of-range scores,
    unknown criterion keys, and invalid normalization parameters are strictly rejected.
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

    class MockReview:
        status = "finalized"

    class MockProject:
        status = "submitted"
        track_id = "trk_01"
        team_id = "tm_01"

    class MockJudge:
        id = "jdg_01"
        account_id = "acc_j"
        tracks = ["trk_01"]

    # 1. Empty rubric criteria
    with pytest.raises(ValueError, match="no criteria defined"):
        calculate_raw_review_score(MockRubric([]), {"c1": 5.0})

    # 2. Zero total weight
    zero_w_rubric = MockRubric([MockCrit("c1", 0.0, 1.0, 5.0)])
    with pytest.raises(ValueError, match="strictly positive"):
        calculate_raw_review_score(zero_w_rubric, {"c1": 3.0})
    with pytest.raises(ValueError, match="strictly positive"):
        calculate_raw_score(zero_w_rubric, {"c1": 3.0})
    elig, reason = evaluate_review_eligibility(MockReview(), MockProject(), MockJudge(), zero_w_rubric, {"c1": 3.0})
    assert elig is False and reason == "rubric_has_zero_weight"

    # 3. Negative weight and non-finite weight
    for bad_w in (-1.0, float("nan"), float("inf"), -float("inf"), True):
        bad_w_rubric = MockRubric([MockCrit("c1", bad_w, 1.0, 5.0), MockCrit("c2", 2.0, 1.0, 5.0)])
        with pytest.raises(ValueError):
            calculate_raw_review_score(bad_w_rubric, {"c1": 3.0, "c2": 3.0})
        with pytest.raises(ValueError):
            calculate_raw_score(bad_w_rubric, {"c1": 3.0, "c2": 3.0})
        elig, reason = evaluate_review_eligibility(
            MockReview(), MockProject(), MockJudge(), bad_w_rubric, {"c1": 3.0, "c2": 3.0}
        )
        assert elig is False and reason == "invalid_weight_criterion_c1"

    # 4. Zero or negative score range (must NEVER substitute 1.0)
    for min_s, max_s in ((5.0, 5.0), (6.0, 5.0), (float("nan"), 5.0), (1.0, float("inf"))):
        bad_range_rubric = MockRubric([MockCrit("c1", 1.0, min_s, max_s)])
        with pytest.raises(ValueError):
            calculate_raw_review_score(bad_range_rubric, {"c1": 5.0})
        with pytest.raises(ValueError):
            calculate_raw_score(bad_range_rubric, {"c1": 5.0})
        elig, reason = evaluate_review_eligibility(
            MockReview(), MockProject(), MockJudge(), bad_range_rubric, {"c1": 5.0}
        )
        assert elig is False and reason == "invalid_range_criterion_c1"

    # 5. Non-finite, boolean, and out-of-range criterion scores
    valid_rubric = MockRubric([MockCrit("c1", 1.0, 1.0, 5.0)])
    for bad_score in (float("nan"), float("inf"), -float("inf"), True, False):
        with pytest.raises(ValueError):
            calculate_raw_review_score(valid_rubric, {"c1": bad_score})
        with pytest.raises(ValueError):
            calculate_raw_score(valid_rubric, {"c1": bad_score})
        elig, reason = evaluate_review_eligibility(
            MockReview(), MockProject(), MockJudge(), valid_rubric, {"c1": bad_score}
        )
        assert elig is False and reason == "non_finite_criterion_c1"

    for out_score in (0.99, 5.01, -10.0, 100.0):
        with pytest.raises(ValueError, match="outside"):
            calculate_raw_review_score(valid_rubric, {"c1": out_score})
        with pytest.raises(ValueError, match="outside"):
            calculate_raw_score(valid_rubric, {"c1": out_score})
        elig, reason = evaluate_review_eligibility(
            MockReview(), MockProject(), MockJudge(), valid_rubric, {"c1": out_score}
        )
        assert elig is False and reason == "out_of_bounds_criterion_c1"

    # 6. Unknown criterion key
    with pytest.raises(ValueError, match="Unknown criterion key 'rogue'"):
        calculate_raw_review_score(valid_rubric, {"c1": 4.0, "rogue": 3.0})
    with pytest.raises(ValueError, match="Unknown criterion key 'rogue'"):
        calculate_raw_score(valid_rubric, {"c1": 4.0, "rogue": 3.0})
    elig, reason = evaluate_review_eligibility(
        MockReview(), MockProject(), MockJudge(), valid_rubric, {"c1": 4.0, "rogue": 3.0}
    )
    assert elig is False and reason == "unknown_criterion_rogue"

    # 7. Normalization parameter validation
    for bad_eps in (0.0, -1e-6, float("nan"), float("inf"), -float("inf"), True, "not_a_float"):
        with pytest.raises(ValueError, match="epsilon"):
            validate_normalization_parameters(min_sample=3, epsilon=bad_eps, method="zscore_v1")

    for bad_min_sample in (0, -2, 2.5, True, False, "abc"):
        with pytest.raises(ValueError, match="min_sample"):
            validate_normalization_parameters(min_sample=bad_min_sample, epsilon=1e-6, method="zscore_v1")

    for bad_method in ("", "unknown_method", None, 123):
        with pytest.raises(ValueError, match="Unsupported normalization method"):
            validate_normalization_parameters(min_sample=3, epsilon=1e-6, method=bad_method)


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

    # 1. Fully eligible (explicit track permission)
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members={"acc_other"},
    )
    assert eligible is True and reason is None

    # 2. Explicit wildcard all-track permission ["*"]
    wildcard_judge = MockJudge()
    wildcard_judge.tracks = ["*"]
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=wildcard_judge,
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members=set(),
    )
    assert eligible is True and reason is None

    # 3. Empty tracks [] or None permits NO tracks (revoked permission)
    for no_tracks in ([], None):
        revoked_judge = MockJudge()
        revoked_judge.tracks = no_tracks
        eligible, reason = evaluate_review_eligibility(
            review=MockReview(),
            project=MockProject(),
            judge=revoked_judge,
            rubric=MockRubric(),
            criteria_scores={"crit_1": 4.0},
            team_members=set(),
        )
        assert eligible is False
        assert reason == "track_mismatch"

    # 4. Different track -> track_mismatch
    other_track_judge = MockJudge()
    other_track_judge.tracks = ["trk_02"]
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=other_track_judge,
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members=set(),
    )
    assert eligible is False and reason == "track_mismatch"

    # 5. Draft review excluded
    draft_rev = MockReview()
    draft_rev.status = "draft"
    eligible, reason = evaluate_review_eligibility(
        review=draft_rev,
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members=set(),
    )
    assert eligible is False and reason == "draft_review"

    # 6. Draft project excluded
    draft_proj = MockProject()
    draft_proj.status = "draft"
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=draft_proj,
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members=set(),
    )
    assert eligible is False and reason == "project_status_draft"

    # 7. Conflict of interest
    eligible, reason = evaluate_review_eligibility(
        review=MockReview(),
        project=MockProject(),
        judge=MockJudge(),
        rubric=MockRubric(),
        criteria_scores={"crit_1": 4.0},
        team_members={"acc_judge", "acc_other"},
    )
    assert eligible is False and reason == "conflict_of_interest"


def test_unified_track_eligibility_across_results_diagnostics_and_csv(db_session):
    """Regression test comparing:
    - Judge permitted for project's track (['trk_01'])
    - Judge permitted only for a different track (['trk_02'])
    - Judge with no track permissions ([] and None)
    - Judge with explicit all-track permission (['*'])
    Verifies that exclusion reasons, raw totals, normalized totals, and CSV exports all agree,
    and that revoking permissions invalidates historical reviews in live results/exports.
    """
    now = datetime.now(timezone.utc)
    evt = Event(
        id="evt_elig",
        name="Eligibility Unification Event",
        submissions_open=now,
        submissions_close=now,
        registration_open=now,
        registration_close=now,
        target_reviews_per_project=2,
    )
    t1 = Track(id="trk_01", event_id=evt.id, name="Track 1")
    t2 = Track(id="trk_02", event_id=evt.id, name="Track 2")
    rub = Rubric(id="rb_elig", event_id=evt.id, name="Rubric", version=1, is_active=True)
    crit = RubricCriterion(
        id="rc_elig", rubric_id=rub.id, key="q", name="Quality", weight=1.0, min_score=0.0, max_score=100.0, order_idx=0
    )
    db_session.add_all([evt, t1, t2, rub, crit])

    j_permitted = Judge(id="jdg_perm", event_id=evt.id, name="Permitted", email="p@e.com", tracks=["trk_01"])
    j_wildcard = Judge(id="jdg_wild", event_id=evt.id, name="Wildcard", email="w@e.com", tracks=["*"])
    j_diff_track = Judge(id="jdg_diff", event_id=evt.id, name="DiffTrack", email="d@e.com", tracks=["trk_02"])
    j_empty_tracks = Judge(id="jdg_empty", event_id=evt.id, name="EmptyTracks", email="e@e.com", tracks=[])
    j_none_tracks = Judge(id="jdg_none", event_id=evt.id, name="NoneTracks", email="n@e.com", tracks=None)
    db_session.add_all([j_permitted, j_wildcard, j_diff_track, j_empty_tracks, j_none_tracks])

    # 3 projects in trk_01 so j_permitted and j_wildcard each have N=3
    projs = []
    for i in range(3):
        tm = Team(id=f"tm_el_{i}", event_id=evt.id, name=f"Team {i}")
        p = Project(
            id=f"prj_el_{i}",
            event_id=evt.id,
            team_id=tm.id,
            track_id="trk_01",
            title=f"Proj {i}",
            summary="s",
            repo_url="r",
            submitted_at=now,
            status="submitted",
        )
        db_session.add_all([tm, p])
        projs.append(p)

    # Eligible judges score all 3 projects:
    # j_permitted: 60, 80, 100 (mean=80, pstdev=16.3299316)
    # j_wildcard:  70, 80, 90  (mean=80, pstdev=8.1649658)
    for i, (s_perm, s_wild) in enumerate([(60.0, 70.0), (80.0, 80.0), (100.0, 90.0)]):
        for j_obj, s_val in [(j_permitted, s_perm), (j_wildcard, s_wild)]:
            db_session.add(
                Review(
                    judge_id=j_obj.id,
                    project_id=projs[i].id,
                    event_id=evt.id,
                    rubric_id=rub.id,
                    criteria={"q": s_val},
                    comment="ok",
                    status="finalized",
                    raw_score=s_val,
                )
            )
            db_session.add(
                ReviewCriterionScore(
                    id=f"rcs_{j_obj.id}_{i}",
                    judge_id=j_obj.id,
                    project_id=projs[i].id,
                    criterion_key="q",
                    score=s_val,
                )
            )

    # Ineligible judges each have a finalized historical review on prj_el_0 with extreme score 10.0
    for bad_j in (j_diff_track, j_empty_tracks, j_none_tracks):
        db_session.add(
            Review(
                judge_id=bad_j.id,
                project_id=projs[0].id,
                event_id=evt.id,
                rubric_id=rub.id,
                criteria={"q": 10.0},
                comment="ineligible",
                status="finalized",
                raw_score=10.0,
            )
        )
        db_session.add(
            ReviewCriterionScore(
                id=f"rcs_{bad_j.id}_0",
                judge_id=bad_j.id,
                project_id=projs[0].id,
                criterion_key="q",
                score=10.0,
            )
        )

    db_session.commit()

    results = calculate_event_results(db_session, evt.id)
    excluded_by_judge = {er["judge_id"]: er["reason"] for er in results["excluded_reviews"]}
    assert excluded_by_judge == {
        "jdg_diff": "track_mismatch",
        "jdg_empty": "track_mismatch",
        "jdg_none": "track_mismatch",
    }

    cohort = results["cohorts"][0]
    assert cohort["cohort_id"] == "trk_01:rb_elig"
    p0 = next(p for p in cohort["projects"] if p["project_id"] == "prj_el_0")
    # Only j_permitted (60) and j_wildcard (70) are included -> raw_mean = 65.0, completed_reviews = 2
    assert p0["completed_reviews"] == 2
    assert math.isclose(p0["raw_mean"], 65.0, rel_tol=1e-9)
    # Both j_permitted and j_wildcard have z = -1.22474487 on prj_el_0 -> normalized_index = -1.22474487
    assert math.isclose(p0["normalized_index"], -20.0 / statistics.pstdev([60.0, 80.0, 100.0]), rel_tol=1e-6)

    # Check CSV agreement (judging.csv and reviews.csv)
    judging_csv = list(csv.DictReader(io.StringIO(generate_judging_results_csv(results))))
    p0_csv = next(r for r in judging_csv if r["project_id"] == "prj_el_0")
    assert p0_csv["raw_mean"] == "65.00"
    assert p0_csv["normalized_index"] == f"{p0['normalized_index']:.4f}"
    assert p0_csv["completed_reviews"] == "2"

    reviews_csv = list(csv.DictReader(io.StringIO(generate_reviews_csv(db_session, evt.id))))
    rev_map = {r["review_id"]: r for r in reviews_csv}
    assert rev_map["jdg_perm:prj_el_0"]["is_eligible"] == "true"
    assert rev_map["jdg_perm:prj_el_0"]["raw_score"] == "60.00"
    assert rev_map["jdg_wild:prj_el_0"]["is_eligible"] == "true"
    assert rev_map["jdg_wild:prj_el_0"]["raw_score"] == "70.00"

    for bad_jid in ("jdg_diff", "jdg_empty", "jdg_none"):
        row = rev_map[f"{bad_jid}:prj_el_0"]
        assert row["is_eligible"] == "false"
        assert row["ineligibility_reason"] == "track_mismatch"
        assert row["raw_score"] == ""

    # Now revoke j_permitted's track permissions ([]): historical reviews become ineligible in live results
    j_permitted.tracks = []
    db_session.commit()

    results_after_revoke = calculate_event_results(db_session, evt.id)
    excluded_judges_after = {er["judge_id"] for er in results_after_revoke["excluded_reviews"]}
    assert "jdg_perm" in excluded_judges_after
    p0_after = next(p for p in results_after_revoke["cohorts"][0]["projects"] if p["project_id"] == "prj_el_0")
    assert p0_after["completed_reviews"] == 1
    assert math.isclose(p0_after["raw_mean"], 70.0, rel_tol=1e-9)


def test_bipartite_connectivity_analysis():
    """Verify connected components detection and warning logic."""
    res1 = analyze_bipartite_connectivity(
        project_ids=["p1", "p2", "p3"],
        usable_judge_evals=[("j1", "p1"), ("j1", "p2"), ("j2", "p2"), ("j2", "p3")],
    )
    assert res1["is_connected"] is True
    assert res1["component_count"] == 1
    assert res1["warning"] is None

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
    crit = RubricCriterion(
        id="rc_test_m", rubric_id=rub.id, key="score", name="Score", weight=1.0, min_score=0.0, max_score=100.0, order_idx=0
    )
    db_session.add(crit)

    judge = Judge(id="jdg_test_math", event_id=evt.id, name="Math Judge", email="math@example.com", tracks=["*"])
    db_session.add(judge)

    scores = [60.0, 80.0, 100.0]
    expected_pstdev = statistics.pstdev(scores)

    for i, s in enumerate(scores):
        team = Team(id=f"tm_math_{i}", event_id=evt.id, name=f"Team {i}")
        db_session.add(team)
        proj = Project(
            id=f"prj_math_{i}",
            event_id=evt.id,
            team_id=team.id,
            track_id="trk_math",
            title=f"Proj {i}",
            summary="s",
            repo_url="r",
            submitted_at=now,
            status="submitted",
        )
        db_session.add(proj)
        rev = Review(
            judge_id=judge.id,
            project_id=proj.id,
            event_id=evt.id,
            rubric_id=rub.id,
            criteria={"score": s},
            comment="good",
            status="finalized",
            raw_score=s,
        )
        db_session.add(rev)
        rc = ReviewCriterionScore(
            id=f"rcs_m_{i}", judge_id=judge.id, project_id=proj.id, criterion_key="score", score=s
        )
        db_session.add(rc)

    db_session.commit()

    results = calculate_event_results(db_session, evt.id, min_sample=3, epsilon=1e-6)
    cohort = results["cohorts"][0]
    j_stat = next(j for j in cohort["judges"] if j["judge_id"] == judge.id)

    assert j_stat["sample_count_N"] == 3
    assert math.isclose(j_stat["mean_score"], 80.0, rel_tol=1e-6)
    assert math.isclose(j_stat["population_std_dev"], expected_pstdev, rel_tol=1e-6)
    assert j_stat["is_usable"] is True

    p0 = next(p for p in cohort["projects"] if p["project_id"] == "prj_math_0")
    p1 = next(p for p in cohort["projects"] if p["project_id"] == "prj_math_1")
    p2 = next(p for p in cohort["projects"] if p["project_id"] == "prj_math_2")

    assert math.isclose(p0["normalized_index"], -20.0 / expected_pstdev, rel_tol=1e-6)
    assert math.isclose(p1["normalized_index"], 0.0, abs_tol=1e-6)
    assert math.isclose(p2["normalized_index"], 20.0 / expected_pstdev, rel_tol=1e-6)


def test_near_zero_variance_and_small_sample_guards(db_session):
    """Verify that judges with N < 3 or near-zero variance are excluded from normalization."""
    now = datetime.now(timezone.utc)
    evt = Event(
        id="evt_guards",
        name="Guards Event",
        submissions_open=now,
        submissions_close=now,
        registration_open=now,
        registration_close=now,
        target_reviews_per_project=3,
    )
    db_session.add(evt)
    rub = Rubric(id="rb_guards", event_id=evt.id, name="R", version=1, is_active=True)
    db_session.add(rub)
    crit = RubricCriterion(
        id="rc_guards", rubric_id=rub.id, key="s", name="S", weight=1.0, min_score=0.0, max_score=10.0, order_idx=0
    )
    db_session.add(crit)

    j_small = Judge(id="jdg_small", event_id=evt.id, name="Small N", email="s@e.com", tracks=["*"])
    j_zero = Judge(id="jdg_zero", event_id=evt.id, name="Zero Var", email="z@e.com", tracks=["*"])
    db_session.add_all([j_small, j_zero])

    team = Team(id="tm_g1", event_id=evt.id, name="Team G")
    db_session.add(team)
    for i in range(4):
        p = Project(
            id=f"prj_g_{i}",
            event_id=evt.id,
            team_id=team.id,
            track_id="trk_g",
            title=f"P {i}",
            summary="s",
            repo_url="r",
            submitted_at=now,
            status="submitted",
        )
        db_session.add(p)

    for i, s in enumerate([5.0, 8.0]):
        db_session.add(
            Review(
                judge_id=j_small.id,
                project_id=f"prj_g_{i}",
                event_id=evt.id,
                rubric_id=rub.id,
                criteria={"s": s},
                comment="",
                status="finalized",
                raw_score=s * 10,
            )
        )
        db_session.add(
            ReviewCriterionScore(id=f"rcs_s_{i}", judge_id=j_small.id, project_id=f"prj_g_{i}", criterion_key="s", score=s)
        )

    for i in range(4):
        db_session.add(
            Review(
                judge_id=j_zero.id,
                project_id=f"prj_g_{i}",
                event_id=evt.id,
                rubric_id=rub.id,
                criteria={"s": 7.0},
                comment="",
                status="finalized",
                raw_score=70.0,
            )
        )
        db_session.add(
            ReviewCriterionScore(id=f"rcs_z_{i}", judge_id=j_zero.id, project_id=f"prj_g_{i}", criterion_key="s", score=7.0)
        )

    db_session.commit()

    results = calculate_event_results(db_session, evt.id, min_sample=3, epsilon=1e-6)
    cohort = results["cohorts"][0]

    j_small_stat = next(j for j in cohort["judges"] if j["judge_id"] == j_small.id)
    assert j_small_stat["is_usable"] is False
    assert "insufficient_samples" in j_small_stat["unusable_reason"]

    j_zero_stat = next(j for j in cohort["judges"] if j["judge_id"] == j_zero.id)
    assert j_zero_stat["is_usable"] is False
    assert "near_zero_variance" in j_zero_stat["unusable_reason"]

    for p in cohort["projects"]:
        assert p["normalized_index"] is None
        assert p["is_provisional"] is True
        assert p["raw_mean"] is not None


def test_comprehensive_snapshot_staleness_and_reproducibility(db_session):
    """Verify snapshot reproducibility from v2 manifest, staleness detection across all
    calculation-affecting domains, non-staleness for irrelevant changes, and legacy snapshot handling.
    """
    now = datetime.now(timezone.utc)
    evt = Event(
        id="evt_snap",
        name="Snap Event",
        submissions_open=now,
        submissions_close=now,
        registration_open=now,
        registration_close=now,
        target_reviews_per_project=1,
    )
    evt_other = Event(
        id="evt_other",
        name="Other Event",
        submissions_open=now,
        submissions_close=now,
        registration_open=now,
        registration_close=now,
        target_reviews_per_project=1,
    )
    acc_j = Account(id="acc_j_snap", email="j@e.com", name="J", password_hash="h")
    rub = Rubric(id="rb_snap", event_id=evt.id, name="R", version=1, is_active=True)
    crit = RubricCriterion(
        id="rc_snap", rubric_id=rub.id, key="s", name="S", weight=1.0, min_score=0.0, max_score=10.0, order_idx=0
    )
    judge = Judge(id="jdg_snap", event_id=evt.id, account_id=acc_j.id, name="Judge", email="j@e.com", tracks=["trk_s"])
    team = Team(id="tm_snap", event_id=evt.id, name="Team")
    proj = Project(
        id="prj_snap",
        event_id=evt.id,
        team_id=team.id,
        track_id="trk_s",
        title="P",
        tagline="Old tagline",
        summary="Old summary",
        repo_url="https://old.example.com",
        submitted_at=now,
        status="submitted",
    )
    rev = Review(
        judge_id=judge.id,
        project_id=proj.id,
        event_id=evt.id,
        rubric_id=rub.id,
        criteria={"s": 5.0},
        comment="ok",
        status="finalized",
        raw_score=50.0,
    )
    rc = ReviewCriterionScore(id="rcs_snap", judge_id=judge.id, project_id=proj.id, criterion_key="s", score=5.0)
    db_session.add_all([evt, evt_other, acc_j, rub, crit, judge, team, proj, rev, rc])
    db_session.commit()

    # 1. Create snapshot
    snapshot = create_results_snapshot(db_session, evt.id, creator_account=None)
    assert snapshot.input_review_hashes.get("manifest_version") == 2

    freshness = check_snapshot_staleness(db_session, snapshot)
    assert freshness["is_stale"] is False
    assert freshness["is_legacy_unverifiable"] is False
    assert freshness["stale_reasons"] == []

    # 2. Irrelevant / non-calculation changes must NOT mark snapshot stale
    proj.summary = "Updated non-scoring summary"
    proj.repo_url = "https://new-repo.example.com"
    judge.name = "Renamed Judge Display Name"
    evt_other.target_reviews_per_project = 99
    db_session.commit()

    still_fresh = check_snapshot_staleness(db_session, snapshot)
    assert still_fresh["is_stale"] is False

    # 3. Rubric weight / range / version change -> STALE
    crit.weight = 2.5
    db_session.commit()
    st_rub = check_snapshot_staleness(db_session, snapshot)
    assert st_rub["is_stale"] is True
    assert any("Rubric criteria, weights, score ranges, or version changed" in r for r in st_rub["stale_reasons"])
    crit.weight = 1.0
    db_session.commit()

    # 4. Judge permission change -> STALE
    judge.tracks = []
    db_session.commit()
    st_j = check_snapshot_staleness(db_session, snapshot)
    assert st_j["is_stale"] is True
    assert any("Judge track permissions or account bindings changed" in r for r in st_j["stale_reasons"])
    judge.tracks = ["trk_s"]
    db_session.commit()

    # 5. Conflict-relevant team membership change -> STALE
    tm_mem = TeamMembership(id="tmm_conflict", team_id=team.id, account_id=acc_j.id, event_id=evt.id, role="member")
    db_session.add(tm_mem)
    db_session.commit()
    st_tm = check_snapshot_staleness(db_session, snapshot)
    assert st_tm["is_stale"] is True
    assert any("Conflict-relevant team memberships changed" in r for r in st_tm["stale_reasons"])
    db_session.delete(tm_mem)
    db_session.commit()

    # 6. Project status or track change -> STALE
    proj.status = "draft"
    db_session.commit()
    st_proj = check_snapshot_staleness(db_session, snapshot)
    assert st_proj["is_stale"] is True
    assert any("Project status, track, or team changed" in r for r in st_proj["stale_reasons"])
    proj.status = "submitted"
    db_session.commit()

    # 7. Event coverage target change -> STALE
    evt.target_reviews_per_project = 4
    db_session.commit()
    st_evt = check_snapshot_staleness(db_session, snapshot)
    assert st_evt["is_stale"] is True
    assert any("target_reviews_per_project changed" in r for r in st_evt["stale_reasons"])
    evt.target_reviews_per_project = 1
    db_session.commit()

    # 8. Method parameter difference -> STALE
    st_param = check_snapshot_staleness(db_session, snapshot, check_parameters={"min_sample": 5, "epsilon": 1e-6})
    assert st_param["is_stale"] is True
    assert any("Normalization method or parameters changed" in r for r in st_param["stale_reasons"])

    # 9. Review score modification -> STALE, yet snapshot still reproduces original results!
    rc.score = 9.0
    db_session.commit()
    st_rev = check_snapshot_staleness(db_session, snapshot)
    assert st_rev["is_stale"] is True
    assert any("review(s) updated since snapshot" in r for r in st_rev["stale_reasons"])

    reproduced = reproduce_snapshot_results(snapshot)
    assert reproduced == snapshot.results_data
    orig_proj = reproduced["cohorts"][0]["projects"][0]
    assert math.isclose(orig_proj["raw_mean"], 50.0, rel_tol=1e-9)

    # 10. Legacy snapshot without manifest_version=2 is flagged as legacy_unverifiable
    legacy_snap = EventResultsSnapshot(
        id="snp_legacy_test",
        event_id=evt.id,
        method="zscore_v1",
        parameters={"min_sample": 3, "epsilon": 1e-6},
        input_review_hashes={"jdg_snap:prj_snap": {"status": "finalized", "criteria_hash": "abc"}},
        results_data={},
        created_at=now,
    )
    db_session.add(legacy_snap)
    db_session.commit()
    legacy_status = check_snapshot_staleness(db_session, legacy_snap)
    assert legacy_status["is_stale"] is True
    assert legacy_status["is_legacy_unverifiable"] is True
    assert legacy_status["provenance_status"] == "legacy_unverifiable"
    with pytest.raises(ValueError, match="legacy format"):
        reproduce_snapshot_results(legacy_snap)


def test_cohorts_track_selection_rubric_versions_and_disconnected_ranking(db_session):
    """Test comparable (event_id, track_id, rubric_id) default cohorts, track_id='all' diagnostic pooling,
    multiple rubric versions, disconnected component-local ranks, and provisional coverage flags.
    """
    now = datetime.now(timezone.utc)
    evt = Event(
        id="evt_cohorts",
        name="Cohorts Event",
        submissions_open=now,
        submissions_close=now,
        registration_open=now,
        registration_close=now,
        target_reviews_per_project=2,
    )
    trk_a = Track(id="trk_a", event_id=evt.id, name="Track A")
    trk_b = Track(id="trk_b", event_id=evt.id, name="Track B")
    rb_v1 = Rubric(id="rb_v1", event_id=evt.id, name="Rubric v1", version=1, is_active=False)
    rb_v2 = Rubric(id="rb_v2", event_id=evt.id, name="Rubric v2", version=2, is_active=True)
    c_v1 = RubricCriterion(id="rc_v1", rubric_id=rb_v1.id, key="q", name="Q", weight=1.0, min_score=0.0, max_score=100.0, order_idx=0)
    c_v2 = RubricCriterion(id="rc_v2", rubric_id=rb_v2.id, key="q", name="Q", weight=1.0, min_score=0.0, max_score=100.0, order_idx=0)
    db_session.add_all([evt, trk_a, trk_b, rb_v1, rb_v2, c_v1, c_v2])

    # Track A judges (only permitted for trk_a)
    ja1 = Judge(id="ja1", event_id=evt.id, name="Judge A1", email="ja1@e.com", tracks=["trk_a"])
    ja2 = Judge(id="ja2", event_id=evt.id, name="Judge A2", email="ja2@e.com", tracks=["trk_a"])
    # Track B judge (only permitted for trk_b)
    jb1 = Judge(id="jb1", event_id=evt.id, name="Judge B1", email="jb1@e.com", tracks=["trk_b"])
    db_session.add_all([ja1, ja2, jb1])

    # 4 projects in trk_a, 3 projects in trk_b (under rb_v1), 1 project in trk_b (under rb_v2)
    for pid, tid in [
        ("pa1", "trk_a"), ("pa2", "trk_a"), ("pa3", "trk_a"), ("pa4", "trk_a"),
        ("pb1", "trk_b"), ("pb2", "trk_b"), ("pb3", "trk_b"), ("pb4", "trk_b"),
    ]:
        tm = Team(id=f"tm_{pid}", event_id=evt.id, name=f"Team {pid}")
        p = Project(
            id=pid,
            event_id=evt.id,
            team_id=tm.id,
            track_id=tid,
            title=f"Project {pid}",
            summary="s",
            repo_url="r",
            submitted_at=now,
            status="submitted",
        )
        db_session.add_all([tm, p])

    # Reviews in trk_a under rb_v1:
    # ja1 scores pa1(90), pa2(70), pa3(55) -> N=3, usable
    # ja2 scores pa1(80), pa2(65), pa4(30) -> N=3, usable (overlaps on pa1, pa2 -> trk_a is 1 connected component)
    # Note: pa3 and pa4 only have 1 usable review (< target 2) -> is_provisional = True
    evals_spec = [
        (ja1, "pa1", rb_v1, 90.0),
        (ja1, "pa2", rb_v1, 70.0),
        (ja1, "pa3", rb_v1, 55.0),
        (ja2, "pa1", rb_v1, 80.0),
        (ja2, "pa2", rb_v1, 65.0),
        (ja2, "pa4", rb_v1, 30.0),
        # Reviews in trk_b under rb_v1: jb1 scores pb1(95), pb2(85), pb3(75) -> N=3, usable (disconnected from trk_a!)
        (jb1, "pb1", rb_v1, 95.0),
        (jb1, "pb2", rb_v1, 85.0),
        (jb1, "pb3", rb_v1, 75.0),
        # Review in trk_b under rb_v2: jb1 scores pb4(88)
        (jb1, "pb4", rb_v2, 88.0),
    ]
    for idx, (j_obj, pid, rub_obj, score_val) in enumerate(evals_spec):
        db_session.add(
            Review(
                judge_id=j_obj.id,
                project_id=pid,
                event_id=evt.id,
                rubric_id=rub_obj.id,
                criteria={"q": score_val},
                comment="ok",
                status="finalized",
                raw_score=score_val,
            )
        )
        db_session.add(
            ReviewCriterionScore(
                id=f"rcs_c_{idx}",
                judge_id=j_obj.id,
                project_id=pid,
                criterion_key="q",
                score=score_val,
            )
        )
    db_session.commit()

    # 1. Default calculation: comparable (track_id, rubric_id) cohorts come FIRST
    res_default = calculate_event_results(db_session, evt.id)
    cohort_ids = [c["cohort_id"] for c in res_default["cohorts"]]
    assert cohort_ids[:3] == ["trk_a:rb_v1", "trk_b:rb_v1", "trk_b:rb_v2"]
    assert cohort_ids[3:] == ["all:rb_v1", "all:rb_v2"]

    # Check trk_a:rb_v1 (connected component -> has both normalized_rank and component_normalized_rank)
    c_trk_a = next(c for c in res_default["cohorts"] if c["cohort_id"] == "trk_a:rb_v1")
    assert c_trk_a["is_diagnostic_pooling"] is False
    assert c_trk_a["graph_connectivity"]["is_connected"] is True
    pa1_row = next(p for p in c_trk_a["projects"] if p["project_id"] == "pa1")
    pa3_row = next(p for p in c_trk_a["projects"] if p["project_id"] == "pa3")
    assert pa1_row["normalized_rank"] == 1
    assert pa1_row["component_normalized_rank"] == 1
    assert pa1_row["is_provisional"] is False
    assert pa3_row["is_provisional"] is True  # usable_normalized_count=1 < target=2

    # 2. Specific track selection: track_id="trk_a"
    res_trk_a = calculate_event_results(db_session, evt.id, track_id="trk_a")
    assert [c["cohort_id"] for c in res_trk_a["cohorts"]] == ["trk_a:rb_v1"]

    # 3. All-tracks diagnostic selection: track_id="all", rubric_id="rb_v1"
    res_all = calculate_event_results(db_session, evt.id, track_id="all", rubric_id="rb_v1")
    assert len(res_all["cohorts"]) == 1
    c_all = res_all["cohorts"][0]
    assert c_all["cohort_id"] == "all:rb_v1"
    assert c_all["is_diagnostic_pooling"] is True
    assert c_all["graph_connectivity"]["is_connected"] is False
    assert c_all["graph_connectivity"]["component_count"] == 2

    # In disconnected cohort, global normalized_rank MUST be None, and component_normalized_rank is 1..N per component
    for p in c_all["projects"]:
        if p["normalized_index"] is not None:
            assert p["normalized_rank"] is None
            assert p["component_normalized_rank"] is not None
            assert p["component_id"] in ("Component 1", "Component 2")

    comp1_ranks = sorted(
        p["component_normalized_rank"] for p in c_all["projects"] if p["component_id"] == "Component 1"
    )
    comp2_ranks = sorted(
        p["component_normalized_rank"] for p in c_all["projects"] if p["component_id"] == "Component 2"
    )
    assert comp1_ranks == [1, 2, 3, 4]
    assert comp2_ranks == [1, 2, 3]

    # Verify CSV output for disconnected cohort withholds global normalized_rank and outputs component_normalized_rank
    csv_rows = list(csv.DictReader(io.StringIO(generate_judging_results_csv(res_all))))
    pa1_csv = next(r for r in csv_rows if r["project_id"] == "pa1")
    pb1_csv = next(r for r in csv_rows if r["project_id"] == "pb1")
    assert pa1_csv["normalized_rank"] == ""
    assert pa1_csv["component_id"] == "Component 1"
    assert pa1_csv["component_normalized_rank"] == "1"
    assert pa1_csv["rank"] == "Component 1 #1"
    assert pb1_csv["normalized_rank"] == ""
    assert pb1_csv["component_id"] == "Component 2"
    assert pb1_csv["component_normalized_rank"] == "1"
    assert pb1_csv["rank"] == "Component 2 #1"
