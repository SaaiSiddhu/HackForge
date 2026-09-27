import hashlib
import json
import math
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .judging_services import is_judge_permitted_for_track
from .models import (
    Account,
    Event,
    EventResultsSnapshot,
    Judge,
    JudgeAssignment,
    Project,
    Review,
    ReviewCriterionScore,
    Rubric,
    Team,
    TeamMembership,
    Track,
)

SUPPORTED_NORMALIZATION_METHODS = {"zscore_v1"}
MANIFEST_VERSION = 2
COHORT_POLICY_VERSION = "per_track_comparable_v1"


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """Read attribute from either a dict or an ORM/object instance."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def validate_normalization_parameters(
    min_sample: Any = 3,
    epsilon: Any = 1e-6,
    method: Any = "zscore_v1",
) -> tuple[int, float, str]:
    """Validate normalization parameters, rejecting invalid types, ranges, NaN, or inf."""
    if not isinstance(method, str) or method not in SUPPORTED_NORMALIZATION_METHODS:
        raise ValueError(
            f"Unsupported normalization method '{method}'. Supported methods: {sorted(SUPPORTED_NORMALIZATION_METHODS)}."
        )

    if isinstance(min_sample, bool) or isinstance(min_sample, float):
        raise ValueError("min_sample must be an integer >= 1.")
    try:
        min_sample_int = int(min_sample)
    except (ValueError, TypeError):
        raise ValueError("min_sample must be an integer >= 1.")
    if str(min_sample).strip() != str(min_sample_int) and not isinstance(min_sample, int):
        raise ValueError("min_sample must be an integer >= 1.")
    if min_sample_int < 1:
        raise ValueError("min_sample must be an integer >= 1.")

    if isinstance(epsilon, bool):
        raise ValueError("epsilon must be a finite number strictly greater than 0.")
    try:
        epsilon_float = float(epsilon)
    except (ValueError, TypeError):
        raise ValueError("epsilon must be a finite number strictly greater than 0.")
    if not math.isfinite(epsilon_float) or epsilon_float <= 0.0:
        raise ValueError("epsilon must be a finite number strictly greater than 0.")

    return min_sample_int, epsilon_float, method


def calculate_raw_review_score(rubric: Any, criterion_scores: dict[str, float]) -> float:
    """Recalculate raw review score on a 0-100 scale using full float precision.

    Formula:
        100 * sum(w_i * (s_i - min_i) / (max_i - min_i)) / sum(w_i)

    Strictly validates:
    - Every criterion weight is finite and non-negative.
    - Total rubric weight is finite and strictly positive.
    - Every criterion has finite min_score and max_score with max_score > min_score.
    - No unknown criterion keys exist in criterion_scores.
    - Every required criterion score is present, finite, and within [min_score, max_score].
    """
    criteria = _attr(rubric, "criteria") if rubric else None
    if not rubric or not criteria:
        raise ValueError("Rubric has no criteria defined.")
    if not isinstance(criterion_scores, dict):
        raise ValueError("criterion_scores must be a dictionary.")

    crit_by_key = {}
    total_weight = 0.0

    for crit in criteria:
        c_key = _attr(crit, "key")
        raw_w = _attr(crit, "weight")
        raw_min = _attr(crit, "min_score")
        raw_max = _attr(crit, "max_score")

        if isinstance(raw_w, bool) or isinstance(raw_min, bool) or isinstance(raw_max, bool):
            raise ValueError(f"Criterion '{c_key}' has invalid boolean weight or score bounds.")

        try:
            w = float(raw_w)
            min_s = float(raw_min)
            max_s = float(raw_max)
        except (ValueError, TypeError):
            raise ValueError(f"Criterion '{c_key}' has non-numeric weight or score bounds.")

        if not math.isfinite(w) or w < 0.0:
            raise ValueError(f"Criterion '{c_key}' weight must be finite and non-negative.")

        denom = max_s - min_s
        if not math.isfinite(min_s) or not math.isfinite(max_s) or not math.isfinite(denom) or denom <= 0.0:
            raise ValueError(f"Criterion '{c_key}' must have finite min_score < max_score.")

        total_weight += w
        crit_by_key[c_key] = (w, min_s, max_s, denom)

    if not math.isfinite(total_weight) or total_weight <= 0.0:
        raise ValueError("Rubric total weight must be strictly positive and finite.")

    # Reject unknown criterion keys
    for k in criterion_scores:
        if k not in crit_by_key:
            raise ValueError(f"Unknown criterion key '{k}' in criterion_scores.")

    weighted_sum = 0.0
    for c_key, (w, min_s, max_s, denom) in crit_by_key.items():
        if c_key not in criterion_scores or criterion_scores[c_key] is None:
            raise ValueError(f"Missing score for criterion '{c_key}'.")

        raw_val = criterion_scores[c_key]
        if isinstance(raw_val, bool):
            raise ValueError(f"Invalid boolean score for criterion '{c_key}'.")

        try:
            score_val = float(raw_val)
        except (ValueError, TypeError):
            raise ValueError(f"Non-numeric score for criterion '{c_key}'.")

        if not math.isfinite(score_val):
            raise ValueError(f"Non-finite score for criterion '{c_key}'.")

        if score_val < min_s or score_val > max_s:
            raise ValueError(
                f"Score for criterion '{c_key}' ({score_val}) is outside allowed range [{min_s}, {max_s}]."
            )

        term = (score_val - min_s) / denom
        weighted_sum += w * term

    result = 100.0 * (weighted_sum / total_weight)
    if not math.isfinite(result):
        raise ValueError("Calculated raw score is non-finite.")
    return result


def evaluate_review_eligibility(
    review: Any,
    project: Optional[Any],
    judge: Optional[Any],
    rubric: Optional[Any],
    criteria_scores: dict[str, float],
    team_members: Optional[set[str]] = None,
) -> tuple[bool, Optional[str]]:
    """Determine whether a review is eligible for scoring, normalization, and exports.

    Centralized Eligibility & Revocation Policy:
    - Review must be 'finalized'.
    - Project must exist and have status 'submitted'.
    - Judge must exist and be permitted for the project's track under `is_judge_permitted_for_track`:
      * `["*"]` permits all tracks.
      * Explicit track IDs permit those tracks.
      * `[]` or `None` permits NO tracks. Revoking a judge's track permissions invalidates their
        historical reviews in live results, diagnostics, and exports (`track_mismatch`), while
        previously frozen snapshots retain the eligibility state captured at snapshot time.
    - Judge must not be a member of the project's team (`conflict_of_interest`).
    - Bound rubric must exist and have valid finite weights (each >= 0, sum > 0) and ranges (max > min).
    - `criteria_scores` must contain no unknown criterion keys and must have finite in-bounds values
      for every criterion in the bound rubric.

    Returns (is_eligible, ineligibility_reason).
    """
    if _attr(review, "status") != "finalized":
        return False, "draft_review"

    if not project:
        return False, "project_not_found"

    proj_status = _attr(project, "status")
    if proj_status != "submitted":
        return False, f"project_status_{proj_status}"

    if not judge:
        return False, "judge_not_found"

    if not rubric:
        return False, "missing_rubric"

    # Unified track permission check
    proj_track_id = _attr(project, "track_id")
    if not is_judge_permitted_for_track(judge, proj_track_id):
        return False, "track_mismatch"

    # Conflict of interest check (judge on project's team)
    judge_account_id = _attr(judge, "account_id")
    if judge_account_id and team_members and judge_account_id in team_members:
        return False, "conflict_of_interest"

    # Criteria completeness, weights, ranges, and bounds validation
    criteria = _attr(rubric, "criteria")
    if not criteria:
        return False, "rubric_has_no_criteria"

    if not isinstance(criteria_scores, dict):
        return False, "invalid_criteria_scores"

    rubric_total_weight = 0.0
    rubric_keys = set()

    for crit in criteria:
        c_key = _attr(crit, "key")
        rubric_keys.add(c_key)
        raw_w = _attr(crit, "weight")
        raw_min = _attr(crit, "min_score")
        raw_max = _attr(crit, "max_score")

        if isinstance(raw_w, bool):
            return False, f"invalid_weight_criterion_{c_key}"
        try:
            w = float(raw_w)
        except (ValueError, TypeError):
            return False, f"invalid_weight_criterion_{c_key}"
        if not math.isfinite(w) or w < 0.0:
            return False, f"invalid_weight_criterion_{c_key}"

        if isinstance(raw_min, bool) or isinstance(raw_max, bool):
            return False, f"invalid_range_criterion_{c_key}"
        try:
            min_s = float(raw_min)
            max_s = float(raw_max)
        except (ValueError, TypeError):
            return False, f"invalid_range_criterion_{c_key}"
        denom = max_s - min_s
        if not math.isfinite(min_s) or not math.isfinite(max_s) or not math.isfinite(denom) or denom <= 0.0:
            return False, f"invalid_range_criterion_{c_key}"

        rubric_total_weight += w

    if not math.isfinite(rubric_total_weight) or rubric_total_weight <= 0.0:
        return False, "rubric_has_zero_weight"

    # Reject unknown criterion keys
    for k in criteria_scores:
        if k not in rubric_keys:
            return False, f"unknown_criterion_{k}"

    for crit in criteria:
        c_key = _attr(crit, "key")
        if c_key not in criteria_scores:
            return False, f"missing_criterion_{c_key}"
        val = criteria_scores[c_key]
        if val is None or isinstance(val, bool):
            return False, f"non_finite_criterion_{c_key}"
        try:
            val_f = float(val)
        except (ValueError, TypeError):
            return False, f"non_finite_criterion_{c_key}"
        if not math.isfinite(val_f):
            return False, f"non_finite_criterion_{c_key}"

        min_s = float(_attr(crit, "min_score"))
        max_s = float(_attr(crit, "max_score"))
        if val_f < min_s or val_f > max_s:
            return False, f"out_of_bounds_criterion_{c_key}"

    return True, None


def analyze_bipartite_connectivity(
    project_ids: list[str],
    usable_judge_evals: list[tuple[str, str]],  # (judge_id, project_id)
) -> dict[str, Any]:
    """Detect connected components in bipartite evaluation graph (projects <-> usable judges).

    Warns prominently when multiple disjoint evaluation networks exist.
    """
    adj = defaultdict(set)
    active_projects = set(project_ids)

    for j_id, p_id in usable_judge_evals:
        if p_id in active_projects:
            adj[f"p:{p_id}"].add(f"j:{j_id}")
            adj[f"j:{j_id}"].add(f"p:{p_id}")

    visited = set()
    components = []
    project_component_map = {}

    comp_idx = 1
    for p_id in sorted(project_ids):
        node = f"p:{p_id}"
        if node in adj and node not in visited:
            comp_nodes = []
            queue = deque([node])
            visited.add(node)
            while queue:
                curr = queue.popleft()
                comp_nodes.append(curr)
                for neighbor in sorted(adj[curr]):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            comp_projs = sorted([x.split(":", 1)[1] for x in comp_nodes if x.startswith("p:")])
            comp_judges = sorted([x.split(":", 1)[1] for x in comp_nodes if x.startswith("j:")])
            for pid in comp_projs:
                project_component_map[pid] = f"Component {comp_idx}"
            components.append({
                "component_id": f"Component {comp_idx}",
                "projects_count": len(comp_projs),
                "judges_count": len(comp_judges),
                "project_ids": comp_projs,
                "judge_ids": comp_judges,
            })
            comp_idx += 1
        elif node not in adj:
            project_component_map[p_id] = "Isolated (No usable judges)"

    is_connected = (len(components) <= 1)
    warning = None
    if len(components) > 1:
        warning = (
            f"Evaluation network is partitioned into {len(components)} disconnected components among usable judges and projects. "
            "Cross-component normalized z-scores cannot be directly calibrated against each other; "
            "only component-local normalized ranks are reported."
        )

    return {
        "is_connected": is_connected,
        "component_count": len(components),
        "components": components,
        "project_component_map": project_component_map,
        "warning": warning,
    }


def capture_calculation_manifest(
    db: Session,
    event_id: str,
    track_id: Optional[str] = None,
    rubric_id: Optional[str] = None,
    min_sample: int = 3,
    epsilon: float = 1e-6,
    method: str = "zscore_v1",
    use_repeatable_read: bool = False,
    on_manifest_read_step: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Capture a canonical, self-contained calculation manifest in a single consistent read pass.

    When `use_repeatable_read=True` on PostgreSQL, begins a clean REPEATABLE READ transaction
    before reading any rows so that all queries observe a single MVCC point-in-time snapshot.
    """
    min_sample_int, epsilon_float, clean_method = validate_normalization_parameters(
        min_sample=min_sample,
        epsilon=epsilon,
        method=method,
    )

    if use_repeatable_read and db.bind is not None and db.bind.dialect.name == "postgresql":
        if not db.new and not db.dirty and not db.deleted:
            db.commit()
            db.expire_all()
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

    event = db.get(Event, event_id)
    if not event:
        raise ValueError(f"Event '{event_id}' not found.")

    if on_manifest_read_step is not None:
        on_manifest_read_step()

    target_reviews = int(getattr(event, "target_reviews_per_project", 3) or 3)

    tracks = db.scalars(
        select(Track).where(Track.event_id == event_id).order_by(Track.id.asc())
    ).all()

    rubrics = db.scalars(
        select(Rubric).where(Rubric.event_id == event_id).order_by(Rubric.id.asc())
    ).all()
    rubrics_manifest = []
    for r in rubrics:
        sorted_crit = sorted(
            r.criteria or [],
            key=lambda c: (int(getattr(c, "order_idx", 0) or 0), str(getattr(c, "id", ""))),
        )
        rubrics_manifest.append({
            "id": r.id,
            "name": r.name,
            "version": int(r.version),
            "is_active": bool(r.is_active),
            "criteria": [
                {
                    "id": c.id,
                    "key": c.key,
                    "name": c.name,
                    "weight": float(c.weight),
                    "min_score": float(c.min_score),
                    "max_score": float(c.max_score),
                    "order_idx": int(c.order_idx or 0),
                }
                for c in sorted_crit
            ],
        })

    teams = db.scalars(
        select(Team).where(Team.event_id == event_id).order_by(Team.id.asc())
    ).all()
    team_ids = [tm.id for tm in teams]
    memberships_by_team: dict[str, set[str]] = defaultdict(set)
    if team_ids:
        memberships = db.scalars(
            select(TeamMembership).where(TeamMembership.team_id.in_(team_ids))
        ).all()
        for m in memberships:
            memberships_by_team[m.team_id].add(m.account_id)

    teams_manifest = [
        {
            "id": tm.id,
            "name": tm.name,
            "member_account_ids": sorted(memberships_by_team.get(tm.id, set())),
        }
        for tm in teams
    ]

    judges = db.scalars(
        select(Judge).where(Judge.event_id == event_id).order_by(Judge.id.asc())
    ).all()
    judges_manifest = [
        {
            "id": j.id,
            "name": j.name,
            "email": j.email,
            "account_id": j.account_id,
            "tracks": list(j.tracks) if j.tracks is not None else None,
        }
        for j in judges
    ]

    assignments = db.scalars(
        select(JudgeAssignment)
        .where(JudgeAssignment.event_id == event_id)
        .order_by(JudgeAssignment.project_id.asc(), JudgeAssignment.judge_id.asc())
    ).all()
    assignments_manifest = [
        {
            "id": a.id,
            "judge_id": a.judge_id,
            "project_id": a.project_id,
        }
        for a in assignments
    ]

    all_projects = db.scalars(
        select(Project).where(Project.event_id == event_id).order_by(Project.id.asc())
    ).all()
    projects_manifest = [
        {
            "id": p.id,
            "title": p.title,
            "tagline": p.tagline,
            "status": p.status,
            "event_id": p.event_id,
            "track_id": p.track_id,
            "team_id": p.team_id,
        }
        for p in all_projects
    ]

    reviews = db.scalars(
        select(Review)
        .where(Review.event_id == event_id)
        .order_by(Review.project_id.asc(), Review.judge_id.asc())
    ).all()

    all_project_ids = [p.id for p in all_projects]
    all_judge_ids = [j.id for j in judges]
    crit_by_review: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    if all_project_ids or all_judge_ids:
        crit_stmt = select(ReviewCriterionScore)
        if all_project_ids:
            crit_stmt = crit_stmt.where(ReviewCriterionScore.project_id.in_(all_project_ids))
        crit_rows = db.scalars(
            crit_stmt.order_by(
                ReviewCriterionScore.judge_id.asc(),
                ReviewCriterionScore.project_id.asc(),
                ReviewCriterionScore.criterion_key.asc(),
            )
        ).all()
        for cr in crit_rows:
            crit_by_review[(cr.judge_id, cr.project_id)][cr.criterion_key] = float(cr.score)

    reviews_manifest = []
    for rev in reviews:
        c_scores = crit_by_review.get((rev.judge_id, rev.project_id))
        if not c_scores and isinstance(rev.criteria, dict):
            c_scores = {}
            for k, v in rev.criteria.items():
                try:
                    c_scores[k] = float(v)
                except (ValueError, TypeError):
                    c_scores[k] = float("nan")
        c_scores = c_scores or {}
        reviews_manifest.append({
            "review_id": f"{rev.judge_id}:{rev.project_id}",
            "judge_id": rev.judge_id,
            "project_id": rev.project_id,
            "event_id": rev.event_id,
            "status": rev.status,
            "version": int(rev.version or 1),
            "rubric_id": rev.rubric_id,
            "raw_score": float(rev.raw_score) if rev.raw_score is not None else None,
            "updated_at": rev.updated_at.isoformat() if rev.updated_at else "",
            "criterion_scores": {k: c_scores[k] for k in sorted(c_scores.keys())},
        })

    return {
        "manifest_version": MANIFEST_VERSION,
        "cohort_policy": COHORT_POLICY_VERSION,
        "method": clean_method,
        "parameters": {
            "min_sample": min_sample_int,
            "epsilon": epsilon_float,
            "track_id": track_id,
            "rubric_id": rubric_id,
            "method": clean_method,
        },
        "event": {
            "id": event.id,
            "target_reviews_per_project": target_reviews,
        },
        "tracks": [{"id": t.id, "name": t.name} for t in tracks],
        "rubrics": rubrics_manifest,
        "teams": teams_manifest,
        "judges": judges_manifest,
        "assignments": assignments_manifest,
        "projects": projects_manifest,
        "reviews": reviews_manifest,
    }


def calculate_results_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Pure, deterministic calculation of event results from a captured manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a dictionary.")

    params = manifest.get("parameters") or {}
    min_sample, epsilon, method = validate_normalization_parameters(
        min_sample=params.get("min_sample", 3),
        epsilon=params.get("epsilon", 1e-6),
        method=params.get("method", manifest.get("method", "zscore_v1")),
    )
    track_id = params.get("track_id")
    rubric_id = params.get("rubric_id")

    event_info = manifest.get("event") or {}
    event_id = event_info.get("id", "")
    target_reviews = int(event_info.get("target_reviews_per_project", 3))
    if target_reviews < 1:
        raise ValueError("Event target_reviews_per_project must be >= 1.")

    all_projects = manifest.get("projects") or []
    all_project_map = {p["id"]: p for p in all_projects}

    # Filter submitted projects for cohort inclusion (track_id="all" or None includes all tracks)
    submitted_projects = [
        p for p in all_projects
        if p.get("status") == "submitted"
        and (track_id in (None, "all") or p.get("track_id") == track_id)
    ]

    teams = manifest.get("teams") or []
    team_members_by_team = {
        tm["id"]: set(tm.get("member_account_ids") or [])
        for tm in teams
    }
    team_names_by_team = {tm["id"]: tm.get("name", "Unknown") for tm in teams}

    judges = manifest.get("judges") or []
    judge_map = {j["id"]: j for j in judges}

    rubrics = manifest.get("rubrics") or []
    rubric_map = {r["id"]: r for r in rubrics}

    tracks = manifest.get("tracks") or []
    track_names = {t["id"]: t.get("name", t["id"]) for t in tracks}

    reviews = manifest.get("reviews") or []

    # Evaluate review eligibility and calculate unrounded raw scores
    eligible_evaluations = []
    excluded_reviews = []

    for rev in reviews:
        p = all_project_map.get(rev["project_id"])
        # When filtering by a specific track (not None and not "all"), skip reviews for projects outside that track
        if track_id not in (None, "all") and (not p or p.get("track_id") != track_id):
            continue

        # Filter by rubric if requested
        if rubric_id and rev.get("rubric_id") != rubric_id:
            continue

        j = judge_map.get(rev["judge_id"])
        r_rubric = rubric_map.get(rev.get("rubric_id")) if rev.get("rubric_id") else None
        c_scores = rev.get("criterion_scores") or {}
        t_members = team_members_by_team.get(p["team_id"], set()) if p else set()

        is_eligible, ineligibility_reason = evaluate_review_eligibility(
            review=rev,
            project=p,
            judge=j,
            rubric=r_rubric,
            criteria_scores=c_scores,
            team_members=t_members,
        )

        if not is_eligible:
            excluded_reviews.append({
                "review_id": rev.get("review_id", f"{rev['judge_id']}:{rev['project_id']}"),
                "judge_id": rev["judge_id"],
                "judge_name": j["name"] if j else "Unknown",
                "project_id": rev["project_id"],
                "project_title": p["title"] if p else "Unknown",
                "status": rev.get("status"),
                "rubric_id": rev.get("rubric_id"),
                "reason": ineligibility_reason,
            })
            continue

        recalculated_raw = calculate_raw_review_score(r_rubric, c_scores)

        eligible_evaluations.append({
            "review": rev,
            "judge_id": rev["judge_id"],
            "project_id": rev["project_id"],
            "track_id": p.get("track_id") if p else None,
            "rubric_id": r_rubric["id"],
            "recalculated_raw": recalculated_raw,
        })

    # Group eligible evaluations into cohorts.
    # Policy:
    # 1. Comparable (track_id, rubric_id) cohorts are ALWAYS the primary/default cohorts.
    # 2. Whole-event cross-track pooling ("all", rubric_id) is generated as an explicit diagnostic cohort
    #    when track_id is None or track_id == "all".
    cohort_evals: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    default_rubric = None
    if rubric_id and rubric_id in rubric_map:
        default_rubric = rubric_map[rubric_id]
    elif rubrics:
        default_rubric = next((r for r in rubrics if r.get("is_active")), rubrics[0])

    # A. Comparable per-track cohorts (when track_id != "all")
    if track_id != "all":
        for ev in eligible_evaluations:
            t_key = ev["track_id"] or "unassigned"
            if track_id is None or track_id == t_key:
                cohort_evals[(t_key, ev["rubric_id"])].append(ev)

        # Ensure tracks with submitted projects still appear even if they have 0 eligible reviews
        if default_rubric:
            tracks_with_evals = {k[0] for k in cohort_evals.keys()}
            for p in submitted_projects:
                t_key = p.get("track_id") or "unassigned"
                if (track_id is None or track_id == t_key) and t_key not in tracks_with_evals:
                    cohort_evals[(t_key, default_rubric["id"])] = []
                    tracks_with_evals.add(t_key)

    # B. Whole-event diagnostic pooling cohorts (when track_id is None or "all")
    if track_id in (None, "all"):
        for ev in eligible_evaluations:
            cohort_evals[("all", ev["rubric_id"])].append(ev)
        if not any(k[0] == "all" for k in cohort_evals.keys()) and default_rubric:
            if track_id == "all" or not cohort_evals:
                cohort_evals[("all", default_rubric["id"])] = []

    if not cohort_evals and default_rubric:
        cohort_evals[(track_id or "all", default_rubric["id"])] = []

    # Sort cohorts so comparable per-track cohorts come FIRST, and "all" (diagnostic pooling) comes LAST
    sorted_cohort_keys = sorted(
        cohort_evals.keys(),
        key=lambda k: (1 if k[0] == "all" else 0, k[0], k[1]),
    )

    cohort_results = []
    for (c_track, c_rubric_id) in sorted_cohort_keys:
        evals = cohort_evals[(c_track, c_rubric_id)]
        c_rubric = rubric_map.get(c_rubric_id)
        if not c_rubric:
            continue

        is_diagnostic_pooling = (c_track == "all")

        # Filter submitted projects belonging to this cohort
        c_projects = [
            p for p in submitted_projects
            if (c_track == "all" or (p.get("track_id") or "unassigned") == c_track)
        ]

        # A. Calculate per-judge cohort statistics
        judge_scores: dict[str, list[float]] = defaultdict(list)
        for ev in evals:
            judge_scores[ev["judge_id"]].append(ev["recalculated_raw"])

        judge_calibration = []
        usable_judge_stats = {}
        for j in judges:
            scores_list = judge_scores.get(j["id"], [])
            n_j = len(scores_list)
            if n_j == 0:
                mean_j = None
                pstdev_j = None
                is_usable = False
                unusable_reason = "no_completed_evaluations"
            else:
                mean_j = sum(scores_list) / n_j
                pstdev_j = statistics.pstdev(scores_list)
                if n_j < min_sample:
                    is_usable = False
                    unusable_reason = f"insufficient_samples (n={n_j} < {min_sample})"
                elif pstdev_j <= epsilon:
                    is_usable = False
                    unusable_reason = f"near_zero_variance (pstdev={pstdev_j:.6e} <= {epsilon:.6e})"
                else:
                    is_usable = True
                    unusable_reason = None

            stat_obj = {
                "judge_id": j["id"],
                "name": j["name"],
                "email": j["email"],
                "sample_count_N": n_j,
                "mean_score": mean_j,
                "population_std_dev": pstdev_j,
                "is_usable": is_usable,
                "unusable_reason": unusable_reason,
            }
            judge_calibration.append(stat_obj)
            if is_usable:
                usable_judge_stats[j["id"]] = stat_obj

        # B. Calculate z-scores for usable judges
        usable_evals = []
        for ev in evals:
            j_id = ev["judge_id"]
            if j_id in usable_judge_stats:
                st = usable_judge_stats[j_id]
                z_score = (ev["recalculated_raw"] - st["mean_score"]) / st["population_std_dev"]
                ev["z_score"] = z_score
                usable_evals.append((j_id, ev["project_id"]))
            else:
                ev["z_score"] = None

        # C. Graph connectivity analysis
        connectivity = analyze_bipartite_connectivity(
            project_ids=[p["id"] for p in c_projects],
            usable_judge_evals=usable_evals,
        )

        # D. Project aggregations
        project_evals_map = defaultdict(list)
        for ev in evals:
            project_evals_map[ev["project_id"]].append(ev)

        project_results_list = []
        for p in c_projects:
            p_evals = project_evals_map.get(p["id"], [])
            raw_scores = [ev["recalculated_raw"] for ev in p_evals]
            usable_z_scores = [ev["z_score"] for ev in p_evals if ev["z_score"] is not None]

            completed_count = len(raw_scores)
            usable_z_count = len(usable_z_scores)

            raw_mean = (sum(raw_scores) / completed_count) if completed_count > 0 else None
            normalized_index = (sum(usable_z_scores) / usable_z_count) if usable_z_count > 0 else None

            coverage_status = "meets_target" if completed_count >= target_reviews else "under_covered"

            is_provisional = (usable_z_count < target_reviews)
            if usable_z_count == 0:
                provisional_reason = "No usable normalized evaluations (judges lacked sufficient sample size or had zero variance)"
            elif usable_z_count < target_reviews:
                provisional_reason = f"Usable normalized evaluations ({usable_z_count}) is less than target ({target_reviews})"
            else:
                provisional_reason = None

            component_id = connectivity["project_component_map"].get(p["id"], "Unknown")

            project_results_list.append({
                "project_id": p["id"],
                "title": p["title"],
                "tagline": p.get("tagline"),
                "track_id": p.get("track_id"),
                "team_id": p["team_id"],
                "team_name": team_names_by_team.get(p["team_id"], "Unknown"),
                "completed_reviews": completed_count,
                "target_reviews": target_reviews,
                "coverage_status": coverage_status,
                "raw_scores": raw_scores,
                "raw_mean": raw_mean,
                "usable_normalized_count": usable_z_count,
                "normalized_scores": usable_z_scores,
                "normalized_index": normalized_index,
                "is_provisional": is_provisional,
                "provisional_reason": provisional_reason,
                "component_id": component_id,
                "rubric_id": c_rubric_id,
            })

        # E. Ranking with deterministic ID tie-breaking
        # 1. Raw ranking (standard competition ranking: 1, 2, 2, 4)
        sorted_for_raw = sorted(
            project_results_list,
            key=lambda x: (
                0 if x["raw_mean"] is not None else 1,
                -(x["raw_mean"] if x["raw_mean"] is not None else 0.0),
                x["project_id"],
            ),
        )
        current_rank = 1
        for idx, item in enumerate(sorted_for_raw):
            if item["raw_mean"] is None:
                item["raw_rank"] = None
                continue
            if idx > 0 and sorted_for_raw[idx - 1]["raw_mean"] == item["raw_mean"]:
                item["raw_rank"] = sorted_for_raw[idx - 1]["raw_rank"]
            else:
                item["raw_rank"] = current_rank
            current_rank = idx + 2

        # 2. Component-local normalized ranking (always computed within each connected component)
        projects_by_comp: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in project_results_list:
            if item["normalized_index"] is not None:
                projects_by_comp[item["component_id"]].append(item)
            else:
                item["component_normalized_rank"] = None

        for comp_name, comp_items in projects_by_comp.items():
            sorted_comp = sorted(
                comp_items,
                key=lambda x: (-x["normalized_index"], x["project_id"]),
            )
            c_rank = 1
            for idx, item in enumerate(sorted_comp):
                if idx > 0 and sorted_comp[idx - 1]["normalized_index"] == item["normalized_index"]:
                    item["component_normalized_rank"] = sorted_comp[idx - 1]["component_normalized_rank"]
                else:
                    item["component_normalized_rank"] = c_rank
                c_rank = idx + 2

        # 3. Cohort-wide normalized ranking:
        # ONLY assigned when the evaluation network is connected (component_count <= 1).
        # When disconnected (component_count > 1), unqualified global normalized_rank is refused (None)
        # and only component_normalized_rank is reported.
        if connectivity["is_connected"]:
            sorted_for_norm = sorted(
                project_results_list,
                key=lambda x: (
                    0 if x["normalized_index"] is not None else 1,
                    -(x["normalized_index"] if x["normalized_index"] is not None else 0.0),
                    x["project_id"],
                ),
            )
            current_norm_rank = 1
            for idx, item in enumerate(sorted_for_norm):
                if item["normalized_index"] is None:
                    item["normalized_rank"] = None
                    item["normalized_rank_display"] = None
                    continue
                if idx > 0 and sorted_for_norm[idx - 1]["normalized_index"] == item["normalized_index"]:
                    item["normalized_rank"] = sorted_for_norm[idx - 1]["normalized_rank"]
                else:
                    item["normalized_rank"] = current_norm_rank
                item["normalized_rank_display"] = f"#{item['normalized_rank']}"
                current_norm_rank = idx + 2

            final_projects_sorted = sorted(
                project_results_list,
                key=lambda x: (
                    0 if x["normalized_rank"] is not None else 1,
                    x["normalized_rank"] if x["normalized_rank"] is not None else 999999,
                    0 if x["raw_rank"] is not None else 1,
                    x["raw_rank"] if x["raw_rank"] is not None else 999999,
                    x["project_id"],
                ),
            )
        else:
            for item in project_results_list:
                item["normalized_rank"] = None
                if item["component_normalized_rank"] is not None:
                    item["normalized_rank_display"] = f"{item['component_id']} #{item['component_normalized_rank']}"
                else:
                    item["normalized_rank_display"] = None

            final_projects_sorted = sorted(
                project_results_list,
                key=lambda x: (
                    0 if x["component_normalized_rank"] is not None else 1,
                    x["component_id"],
                    x["component_normalized_rank"] if x["component_normalized_rank"] is not None else 999999,
                    0 if x["raw_rank"] is not None else 1,
                    x["raw_rank"] if x["raw_rank"] is not None else 999999,
                    x["project_id"],
                ),
            )

        if is_diagnostic_pooling:
            cohort_name = "All Tracks (Diagnostic Cross-Track Pooling)"
            diagnostic_note = (
                "Diagnostic cross-track pooling: aggregates judge evaluations across all tracks under a shared rubric "
                "to inspect cross-track judge calibration. Primary comparative rankings are per-track."
            )
        else:
            cohort_name = f"Track: {track_names.get(c_track, c_track)}"
            diagnostic_note = None

        cohort_results.append({
            "cohort_id": f"{c_track}:{c_rubric_id}",
            "cohort_name": cohort_name,
            "is_diagnostic_pooling": is_diagnostic_pooling,
            "diagnostic_note": diagnostic_note,
            "track_id": c_track if c_track != "all" else None,
            "track_name": track_names.get(c_track, c_track) if c_track != "all" else "All Tracks (Diagnostic Pooling)",
            "rubric_id": c_rubric["id"],
            "rubric_name": c_rubric["name"],
            "rubric_version": c_rubric["version"],
            "projects": final_projects_sorted,
            "judges": judge_calibration,
            "graph_connectivity": connectivity,
            "summary": {
                "total_projects": len(c_projects),
                "total_eligible_reviews": len(evals),
                "usable_judges_count": len(usable_judge_stats),
                "unusable_judges_count": len(judge_calibration) - len(usable_judge_stats),
            },
        })

    overall_stats = {
        "event_id": event_id,
        "target_reviews_per_project": target_reviews,
        "total_submitted_projects": len(submitted_projects),
        "total_judges": len(judges),
        "total_reviews": len(reviews),
        "total_eligible_reviews": len(eligible_evaluations),
        "total_excluded_reviews": len(excluded_reviews),
        "cohorts_count": len(cohort_results),
    }

    return {
        "event_id": event_id,
        "target_reviews_per_project": target_reviews,
        "cohort_policy": COHORT_POLICY_VERSION,
        "parameters": {
            "min_sample": min_sample,
            "epsilon": epsilon,
            "track_id": track_id,
            "rubric_id": rubric_id,
            "method": method,
        },
        "cohorts": cohort_results,
        "excluded_reviews": excluded_reviews,
        "overall_stats": overall_stats,
    }


def calculate_event_results(
    db: Session,
    event_id: str,
    track_id: Optional[str] = None,
    rubric_id: Optional[str] = None,
    min_sample: int = 3,
    epsilon: float = 1e-6,
    method: str = "zscore_v1",
) -> dict[str, Any]:
    """Compute complete raw and z-score normalized results for an event."""
    manifest = capture_calculation_manifest(
        db=db,
        event_id=event_id,
        track_id=track_id,
        rubric_id=rubric_id,
        min_sample=min_sample,
        epsilon=epsilon,
        method=method,
    )
    return calculate_results_from_manifest(manifest)


def _extract_calculation_state(manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract calculation-affecting state from a manifest for fingerprinting and staleness checks."""
    params = manifest.get("parameters") or {}
    event_info = manifest.get("event") or {}

    rubrics_state = {}
    for r in manifest.get("rubrics") or []:
        rubrics_state[r["id"]] = {
            "version": int(r.get("version", 1)),
            "is_active": bool(r.get("is_active", False)),
            "criteria": [
                {
                    "key": c["key"],
                    "weight": float(c["weight"]),
                    "min_score": float(c["min_score"]),
                    "max_score": float(c["max_score"]),
                }
                for c in (r.get("criteria") or [])
            ],
        }

    judges_state = {}
    for j in manifest.get("judges") or []:
        judges_state[j["id"]] = {
            "account_id": j.get("account_id"),
            "tracks": list(j["tracks"]) if j.get("tracks") is not None else None,
        }

    teams_state = {}
    for tm in manifest.get("teams") or []:
        teams_state[tm["id"]] = sorted(tm.get("member_account_ids") or [])

    projects_state = {}
    for p in manifest.get("projects") or []:
        projects_state[p["id"]] = {
            "status": p.get("status"),
            "event_id": p.get("event_id"),
            "track_id": p.get("track_id"),
            "team_id": p.get("team_id"),
        }

    reviews_state = {}
    for rev in manifest.get("reviews") or []:
        c_scores = rev.get("criterion_scores") or {}
        c_str = json.dumps(c_scores, sort_keys=True)
        c_hash = hashlib.sha256(c_str.encode("utf-8")).hexdigest()[:16]
        r_key = rev.get("review_id") or f"{rev['judge_id']}:{rev['project_id']}"
        reviews_state[r_key] = {
            "status": rev.get("status"),
            "version": int(rev.get("version", 1)),
            "rubric_id": rev.get("rubric_id"),
            "criterion_scores": c_scores,
            "criteria_hash": c_hash,
        }

    return {
        "cohort_policy": manifest.get("cohort_policy", COHORT_POLICY_VERSION),
        "method": manifest.get("method", params.get("method", "zscore_v1")),
        "parameters": {
            "min_sample": int(params.get("min_sample", 3)),
            "epsilon": float(params.get("epsilon", 1e-6)),
            "track_id": params.get("track_id"),
            "rubric_id": params.get("rubric_id"),
            "method": params.get("method", manifest.get("method", "zscore_v1")),
        },
        "event": {
            "id": event_info.get("id"),
            "target_reviews_per_project": int(event_info.get("target_reviews_per_project", 3)),
        },
        "rubrics": rubrics_state,
        "judges": judges_state,
        "teams": teams_state,
        "projects": projects_state,
        "reviews": reviews_state,
    }


def compute_event_review_signatures(db: Session, event_id: str) -> dict[str, Any]:
    """Build a deterministic signature dictionary of all reviews in the event."""
    manifest = capture_calculation_manifest(db=db, event_id=event_id)
    calc_state = _extract_calculation_state(manifest)
    return calc_state["reviews"]


def reproduce_snapshot_results(snapshot: EventResultsSnapshot) -> dict[str, Any]:
    """Deterministically reproduce snapshot results purely from its stored canonical manifest."""
    stored = snapshot.input_review_hashes
    if not isinstance(stored, dict) or stored.get("manifest_version") != MANIFEST_VERSION or "manifest" not in stored:
        raise ValueError(
            f"Snapshot '{snapshot.id}' uses a legacy format without a v{MANIFEST_VERSION} calculation manifest "
            "and cannot be reproduced from manifest provenance alone."
        )
    return calculate_results_from_manifest(stored["manifest"])


def create_results_snapshot(
    db: Session,
    event_id: str,
    creator_account: Optional[Account],
    method: str = "zscore_v1",
    parameters: Optional[dict[str, Any]] = None,
    on_manifest_read_step: Optional[Callable[[], None]] = None,
) -> EventResultsSnapshot:
    """Compute and store an immutable results snapshot from a single consistent-read manifest."""
    raw_params = dict(parameters or {})
    min_sample, epsilon, clean_method = validate_normalization_parameters(
        min_sample=raw_params.get("min_sample", 3),
        epsilon=raw_params.get("epsilon", 1e-6),
        method=raw_params.get("method", method),
    )
    track_id = raw_params.get("track_id")
    rubric_id = raw_params.get("rubric_id")

    manifest = capture_calculation_manifest(
        db=db,
        event_id=event_id,
        track_id=track_id,
        rubric_id=rubric_id,
        min_sample=min_sample,
        epsilon=epsilon,
        method=clean_method,
        use_repeatable_read=True,
        on_manifest_read_step=on_manifest_read_step,
    )

    results_data = calculate_results_from_manifest(manifest)
    calc_state = _extract_calculation_state(manifest)
    manifest_json = json.dumps(calc_state, sort_keys=True)
    manifest_sha256 = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()

    clean_params = {
        "min_sample": min_sample,
        "epsilon": epsilon,
        "track_id": track_id,
        "rubric_id": rubric_id,
        "method": clean_method,
    }

    provenance_bundle = {
        "manifest_version": MANIFEST_VERSION,
        "manifest_sha256": manifest_sha256,
        "cohort_policy": COHORT_POLICY_VERSION,
        "manifest": manifest,
        "reviews": calc_state["reviews"],
    }

    snapshot_id = f"snp_{event_id}_{int(datetime.now(timezone.utc).timestamp())}_{uuid4().hex[:8]}"

    snapshot = EventResultsSnapshot(
        id=snapshot_id,
        event_id=event_id,
        created_by_account_id=creator_account.id if creator_account else None,
        method=clean_method,
        parameters=clean_params,
        input_review_hashes=provenance_bundle,
        results_data=results_data,
        created_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def check_snapshot_staleness(
    db: Session,
    snapshot: EventResultsSnapshot,
    check_parameters: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compare stored snapshot canonical manifest against current live database state.

    Detects changes to:
    - Normalization method or parameters (min_sample, epsilon, track_id, rubric_id, method)
    - Event target_reviews_per_project
    - Rubric criteria, weights, score ranges, or versions
    - Judge track permissions or account bindings
    - Conflict-relevant team memberships
    - Project status, track, or team
    - Review additions, removals, status, rubric bindings, or criterion scores

    Preserves legacy snapshots that lack a v2 manifest, marking them as `legacy_unverifiable`.
    """
    stored_bundle = snapshot.input_review_hashes or {}

    # Detect legacy snapshots without full v2 manifest provenance
    if not isinstance(stored_bundle, dict) or stored_bundle.get("manifest_version") != MANIFEST_VERSION or "manifest" not in stored_bundle:
        stale_reasons = ["Legacy snapshot lacks full v2 calculation manifest (unverifiable provenance)"]
        current_reviews = compute_event_review_signatures(db, snapshot.event_id)
        legacy_reviews = {
            k: v for k, v in (stored_bundle if isinstance(stored_bundle, dict) else {}).items()
            if isinstance(v, dict) and "criteria_hash" in v
        }
        if legacy_reviews:
            curr_keys = set(current_reviews.keys())
            stor_keys = set(legacy_reviews.keys())
            if curr_keys - stor_keys:
                stale_reasons.append(f"{len(curr_keys - stor_keys)} new review(s) submitted since snapshot")
            if stor_keys - curr_keys:
                stale_reasons.append(f"{len(stor_keys - curr_keys)} review(s) removed since snapshot")
            mod_count = sum(
                1 for k in (curr_keys & stor_keys)
                if current_reviews[k].get("status") != legacy_reviews[k].get("status")
                or current_reviews[k].get("criteria_hash") != legacy_reviews[k].get("criteria_hash")
                or current_reviews[k].get("rubric_id") != legacy_reviews[k].get("rubric_id")
            )
            if mod_count > 0:
                stale_reasons.append(f"{mod_count} review(s) updated since snapshot")

        return {
            "is_stale": True,
            "is_legacy_unverifiable": True,
            "provenance_status": "legacy_unverifiable",
            "stale_reasons": stale_reasons,
            "summary": "Legacy snapshot lacks full calculation manifest (unverifiable provenance)",
        }

    stored_manifest = stored_bundle["manifest"]
    stored_state = _extract_calculation_state(stored_manifest)

    snap_params = dict(snapshot.parameters or stored_state["parameters"])
    snap_method = snapshot.method or stored_state["method"]

    # Capture current live state using snapshot's baseline parameters
    current_manifest = capture_calculation_manifest(
        db=db,
        event_id=snapshot.event_id,
        track_id=stored_state["parameters"].get("track_id"),
        rubric_id=stored_state["parameters"].get("rubric_id"),
        min_sample=stored_state["parameters"].get("min_sample", 3),
        epsilon=stored_state["parameters"].get("epsilon", 1e-6),
        method=stored_state["method"],
    )
    current_state = _extract_calculation_state(current_manifest)

    stale_reasons = []

    # 1. Method & parameter comparison (comparing stored manifest vs snapshot.parameters or requested check_parameters)
    effective_params = dict(check_parameters) if check_parameters is not None else snap_params
    effective_method = effective_params.get("method", snap_method)
    if (
        effective_method != stored_state["method"]
        or int(effective_params.get("min_sample", 3)) != stored_state["parameters"]["min_sample"]
        or not math.isclose(float(effective_params.get("epsilon", 1e-6)), stored_state["parameters"]["epsilon"], rel_tol=1e-12, abs_tol=1e-15)
        or effective_params.get("track_id") != stored_state["parameters"].get("track_id")
        or effective_params.get("rubric_id") != stored_state["parameters"].get("rubric_id")
    ):
        stale_reasons.append("Normalization method or parameters changed since snapshot")

    # 2. Event coverage target comparison
    if current_state["event"]["target_reviews_per_project"] != stored_state["event"]["target_reviews_per_project"]:
        stale_reasons.append(
            f"Event target_reviews_per_project changed ({stored_state['event']['target_reviews_per_project']} -> {current_state['event']['target_reviews_per_project']})"
        )

    # 3. Rubric weights, ranges, or versions comparison
    if current_state["rubrics"] != stored_state["rubrics"]:
        stale_reasons.append("Rubric criteria, weights, score ranges, or version changed since snapshot")

    # 4. Judge permissions or account bindings comparison
    if current_state["judges"] != stored_state["judges"]:
        stale_reasons.append("Judge track permissions or account bindings changed since snapshot")

    # 5. Conflict-relevant team memberships comparison
    if current_state["teams"] != stored_state["teams"]:
        stale_reasons.append("Conflict-relevant team memberships changed since snapshot")

    # 6. Project status, track, or team comparison
    if current_state["projects"] != stored_state["projects"]:
        stale_reasons.append("Project status, track, or team changed since snapshot")

    # 7. Reviews comparison (additions, removals, status, rubric_id, criterion_scores)
    current_reviews = current_state["reviews"]
    stored_reviews = stored_state["reviews"]

    current_keys = set(current_reviews.keys())
    stored_keys = set(stored_reviews.keys())

    added_keys = current_keys - stored_keys
    removed_keys = stored_keys - current_keys

    if added_keys:
        stale_reasons.append(f"{len(added_keys)} new review(s) submitted since snapshot")
    if removed_keys:
        stale_reasons.append(f"{len(removed_keys)} review(s) removed since snapshot")

    modified_count = 0
    for k in current_keys & stored_keys:
        curr = current_reviews[k]
        stor = stored_reviews[k]
        if (
            curr.get("status") != stor.get("status")
            or curr.get("rubric_id") != stor.get("rubric_id")
            or curr.get("criteria_hash") != stor.get("criteria_hash")
        ):
            modified_count += 1

    if modified_count > 0:
        stale_reasons.append(f"{modified_count} review(s) updated since snapshot")

    is_stale = (len(stale_reasons) > 0)
    return {
        "is_stale": is_stale,
        "is_legacy_unverifiable": False,
        "provenance_status": "verified_manifest_v2",
        "stale_reasons": stale_reasons,
        "summary": "Snapshot is out of date with live database" if is_stale else "Snapshot is synchronized with live reviews",
    }
