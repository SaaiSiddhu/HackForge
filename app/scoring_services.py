import hashlib
import json
import math
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select, and_, func
from sqlalchemy.orm import Session

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


def calculate_raw_review_score(rubric: Rubric, criterion_scores: dict[str, float]) -> float:
    """Recalculate raw review score on a 0-100 scale using full float precision.
    
    Formula:
        100 * sum(w_i * (s_i - min_i) / (max_i - min_i)) / sum(w_i)
    """
    if not rubric or not rubric.criteria:
        raise ValueError("Rubric has no criteria defined.")

    total_weight = sum(float(c.weight) for c in rubric.criteria)
    if total_weight <= 0:
        raise ValueError("Rubric total weight must be strictly positive.")

    weighted_sum = 0.0
    for crit in rubric.criteria:
        score_val = criterion_scores.get(crit.key)
        if score_val is None:
            raise ValueError(f"Missing score for criterion '{crit.key}'.")
        denom = float(crit.max_score) - float(crit.min_score)
        if denom <= 0:
            term = 1.0
        else:
            term = (float(score_val) - float(crit.min_score)) / denom
        weighted_sum += float(crit.weight) * term

    return 100.0 * (weighted_sum / total_weight)


def evaluate_review_eligibility(
    review: Review,
    project: Optional[Project],
    judge: Optional[Judge],
    rubric: Optional[Rubric],
    criteria_scores: dict[str, float],
    team_members: Optional[set[str]] = None,
) -> tuple[bool, Optional[str]]:
    """Determine whether a review is eligible for scoring and results.
    
    Returns (is_eligible, ineligibility_reason).
    """
    if review.status != "finalized":
        return False, "draft_review"

    if not project:
        return False, "project_not_found"

    if project.status != "submitted":
        return False, f"project_status_{project.status}"

    if not judge:
        return False, "judge_not_found"

    if not rubric:
        return False, "missing_rubric"

    # Track permission check
    if judge.tracks and len(judge.tracks) > 0 and "*" not in judge.tracks:
        if not project.track_id or project.track_id not in judge.tracks:
            return False, "track_mismatch"

    # Conflict of interest check (judge on project's team)
    if judge.account_id and team_members and judge.account_id in team_members:
        return False, "conflict_of_interest"

    # Criteria completeness and bounds validation
    if not rubric.criteria:
        return False, "rubric_has_no_criteria"

    rubric_total_weight = sum(float(c.weight) for c in rubric.criteria)
    if rubric_total_weight <= 0:
        return False, "rubric_has_zero_weight"

    for crit in rubric.criteria:
        if crit.key not in criteria_scores:
            return False, f"missing_criterion_{crit.key}"
        val = criteria_scores[crit.key]
        if val is None or not math.isfinite(val):
            return False, f"non_finite_criterion_{crit.key}"
        if val < float(crit.min_score) or val > float(crit.max_score):
            return False, f"out_of_bounds_criterion_{crit.key}"

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

    # Visit all connected components
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
            comp_projs = [x.split(":", 1)[1] for x in comp_nodes if x.startswith("p:")]
            comp_judges = [x.split(":", 1)[1] for x in comp_nodes if x.startswith("j:")]
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
            # Isolated project with no usable judge evaluations
            project_component_map[p_id] = "Isolated (No usable judges)"

    is_connected = (len(components) <= 1)
    warning = None
    if len(components) > 1:
        warning = (
            f"Evaluation network is partitioned into {len(components)} disconnected components among usable judges and projects. "
            "Cross-component normalized z-scores cannot be directly calibrated against each other."
        )

    return {
        "is_connected": is_connected,
        "component_count": len(components),
        "components": components,
        "project_component_map": project_component_map,
        "warning": warning,
    }


def calculate_event_results(
    db: Session,
    event_id: str,
    track_id: Optional[str] = None,
    rubric_id: Optional[str] = None,
    min_sample: int = 3,
    epsilon: float = 1e-6,
) -> dict[str, Any]:
    """Compute complete raw and z-score normalized results for an event.
    
    Enforces historical rubric integrity, explicit eligibility, population std dev,
    and disconnected evaluation graph warnings.
    """
    event = db.get(Event, event_id)
    if not event:
        raise ValueError(f"Event '{event_id}' not found.")

    target_reviews = getattr(event, "target_reviews_per_project", 3)

    # 1. Load all submitted projects for event
    proj_query = select(Project).where(Project.event_id == event_id, Project.status == "submitted")
    if track_id:
        proj_query = proj_query.where(Project.track_id == track_id)
    projects = db.scalars(proj_query.order_by(Project.id.asc())).all()
    project_map = {p.id: p for p in projects}

    # Preload team members for conflict of interest checks
    teams = db.scalars(select(Team).where(Team.event_id == event_id)).all()
    team_members_by_team = defaultdict(set)
    for tm in teams:
        members = db.scalars(select(TeamMembership).where(TeamMembership.team_id == tm.id)).all()
        for m in members:
            team_members_by_team[tm.id].add(m.account_id)
    team_names_by_team = {tm.id: tm.name for tm in teams}

    # 2. Load judges for event
    judges = db.scalars(select(Judge).where(Judge.event_id == event_id).order_by(Judge.id.asc())).all()
    judge_map = {j.id: j for j in judges}

    # 3. Load rubrics for event
    rubrics = db.scalars(select(Rubric).where(Rubric.event_id == event_id)).all()
    rubric_map = {r.id: r for r in rubrics}

    # 4. Load all reviews and criteria scores for event
    rev_query = select(Review).where(Review.event_id == event_id)
    reviews = db.scalars(rev_query.order_by(Review.project_id.asc(), Review.judge_id.asc())).all()

    crit_rows = db.scalars(
        select(ReviewCriterionScore).where(
            ReviewCriterionScore.project_id.in_(list(project_map.keys()))
        )
    ).all()
    crit_by_review = defaultdict(dict)
    for cr in crit_rows:
        crit_by_review[(cr.judge_id, cr.project_id)][cr.criterion_key] = float(cr.score)

    # 5. Evaluate eligibility and calculate raw scores
    eligible_evaluations = []
    excluded_reviews = []

    for rev in reviews:
        p = project_map.get(rev.project_id)
        # If filtering by track, skip reviews for projects outside the track
        if track_id and (not p or p.track_id != track_id):
            continue

        j = judge_map.get(rev.judge_id)
        r_rubric = rubric_map.get(rev.rubric_id) if rev.rubric_id else None

        # Filter by rubric if requested
        if rubric_id and rev.rubric_id != rubric_id:
            continue

        c_scores = crit_by_review.get((rev.judge_id, rev.project_id), {})
        t_members = team_members_by_team.get(p.team_id, set()) if p else set()

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
                "review_id": f"{rev.judge_id}:{rev.project_id}",
                "judge_id": rev.judge_id,
                "judge_name": j.name if j else "Unknown",
                "project_id": rev.project_id,
                "project_title": p.title if p else "Unknown",
                "status": rev.status,
                "rubric_id": rev.rubric_id,
                "reason": ineligibility_reason,
            })
            continue

        # Recalculate raw score with unrounded float precision
        recalculated_raw = calculate_raw_review_score(r_rubric, c_scores)

        eligible_evaluations.append({
            "review": rev,
            "judge_id": rev.judge_id,
            "project_id": rev.project_id,
            "track_id": p.track_id if p else None,
            "rubric_id": r_rubric.id,
            "recalculated_raw": recalculated_raw,
        })

    # 6. Group eligible evaluations into cohorts
    # Cohort keys are (track_key, rubric_id) where track_key is 'all' or a specific track_id
    cohort_evals = defaultdict(list)

    # A. Whole-event cohorts (if track_id is None or 'all')
    if track_id in (None, "all"):
        for ev in eligible_evaluations:
            cohort_evals[("all", ev["rubric_id"])].append(ev)

    # B. Per-track cohorts (if track_id is None or specific track)
    if track_id != "all":
        for ev in eligible_evaluations:
            t_key = ev["track_id"] or "all"
            if track_id is None or track_id == t_key:
                cohort_evals[(t_key, ev["rubric_id"])].append(ev)

    # If no eligible evaluations exist but projects exist, create an empty cohort entry
    if not cohort_evals and rubrics:
        active_r = next((r for r in rubrics if r.is_active), rubrics[0])
        cohort_evals[(track_id or "all", active_r.id)] = []

    # Preload track names for display
    tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
    track_names = {t.id: t.name for t in tracks}

    cohort_results = []
    # Sort cohorts so "all" appears first, then by track_id
    sorted_cohort_keys = sorted(
        cohort_evals.keys(),
        key=lambda k: (0 if k[0] == "all" else 1, k[0], k[1]),
    )

    for (c_track, c_rubric_id) in sorted_cohort_keys:
        evals = cohort_evals[(c_track, c_rubric_id)]
        c_rubric = rubric_map.get(c_rubric_id)
        if not c_rubric:
            continue

        # Filter projects belonging to this cohort
        c_projects = [
            p for p in projects
            if (c_track == "all" or p.track_id == c_track)
        ]

        # A. Calculate per-judge cohort statistics
        judge_scores = defaultdict(list)
        review_raw_lookup = {}
        for ev in evals:
            judge_scores[ev["judge_id"]].append(ev["recalculated_raw"])
            review_raw_lookup[(ev["judge_id"], ev["project_id"])] = ev["recalculated_raw"]

        judge_calibration = []
        usable_judge_stats = {}
        for j in judges:
            scores_list = judge_scores.get(j.id, [])
            n_j = len(scores_list)
            if n_j == 0:
                mean_j = None
                pstdev_j = None
                is_usable = False
                unusable_reason = "no_completed_evaluations"
            else:
                mean_j = sum(scores_list) / n_j
                # Population standard deviation: sqrt( (1/N) * sum((x - mu)^2) )
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
                "judge_id": j.id,
                "name": j.name,
                "email": j.email,
                "sample_count_N": n_j,
                "mean_score": mean_j,
                "population_std_dev": pstdev_j,
                "is_usable": is_usable,
                "unusable_reason": unusable_reason,
            }
            judge_calibration.append(stat_obj)
            if is_usable:
                usable_judge_stats[j.id] = stat_obj

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
            project_ids=[p.id for p in c_projects],
            usable_judge_evals=usable_evals,
        )

        # D. Project aggregations
        project_evals_map = defaultdict(list)
        for ev in evals:
            project_evals_map[ev["project_id"]].append(ev)

        project_results_list = []
        for p in c_projects:
            p_evals = project_evals_map.get(p.id, [])
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

            component_id = connectivity["project_component_map"].get(p.id, "Unknown")

            project_results_list.append({
                "project_id": p.id,
                "title": p.title,
                "tagline": p.tagline,
                "track_id": p.track_id,
                "team_id": p.team_id,
                "team_name": team_names_by_team.get(p.team_id, "Unknown"),
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

        # E. Ranking with stable ID tie-breaking
        # 1. Raw ranking (standard competition ranking: 1, 2, 2, 4)
        sorted_for_raw = sorted(
            project_results_list,
            key=lambda x: (
                x["raw_mean"] is not None,
                x["raw_mean"] if x["raw_mean"] is not None else -float("inf"),
                # Stable tie break inverted for asc project_id
            ),
            reverse=True,
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

        # 2. Normalized ranking
        sorted_for_norm = sorted(
            project_results_list,
            key=lambda x: (
                x["normalized_index"] is not None,
                x["normalized_index"] if x["normalized_index"] is not None else -float("inf"),
            ),
            reverse=True,
        )
        current_norm_rank = 1
        for idx, item in enumerate(sorted_for_norm):
            if item["normalized_index"] is None:
                item["normalized_rank"] = None
                continue
            if idx > 0 and sorted_for_norm[idx - 1]["normalized_index"] == item["normalized_index"]:
                item["normalized_rank"] = sorted_for_norm[idx - 1]["normalized_rank"]
            else:
                item["normalized_rank"] = current_norm_rank
            current_norm_rank = idx + 2

        # Final display sort: by normalized_rank (if scored), then raw_rank, then project_id
        final_projects_sorted = sorted(
            project_results_list,
            key=lambda x: (
                0 if x["normalized_rank"] is not None else 1,
                x["normalized_rank"] if x["normalized_rank"] is not None else 999999,
                0 if x["raw_rank"] is not None else 1,
                x["raw_rank"] if x["raw_rank"] is not None else 999999,
                x["project_id"],
            )
        )

        cohort_name = "Whole Event" if c_track == "all" else f"Track: {track_names.get(c_track, c_track)}"
        cohort_results.append({
            "cohort_id": f"{c_track}:{c_rubric_id}",
            "cohort_name": cohort_name,
            "track_id": c_track if c_track != "all" else None,
            "track_name": track_names.get(c_track, "All Tracks") if c_track != "all" else "All Tracks",
            "rubric_id": c_rubric.id,
            "rubric_name": c_rubric.name,
            "rubric_version": c_rubric.version,
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

    # Overall event summary
    overall_stats = {
        "event_id": event_id,
        "target_reviews_per_project": target_reviews,
        "total_submitted_projects": len(projects),
        "total_judges": len(judges),
        "total_reviews": len(reviews),
        "total_eligible_reviews": len(eligible_evaluations),
        "total_excluded_reviews": len(excluded_reviews),
        "cohorts_count": len(cohort_results),
    }

    return {
        "event_id": event_id,
        "target_reviews_per_project": target_reviews,
        "parameters": {
            "min_sample": min_sample,
            "epsilon": epsilon,
            "track_id": track_id,
            "rubric_id": rubric_id,
        },
        "cohorts": cohort_results,
        "excluded_reviews": excluded_reviews,
        "overall_stats": overall_stats,
    }


def compute_event_review_signatures(db: Session, event_id: str) -> dict[str, Any]:
    """Build a deterministic signature dictionary of all reviews in the event for OCC snapshot verification."""
    reviews = db.scalars(
        select(Review).where(Review.event_id == event_id).order_by(Review.judge_id.asc(), Review.project_id.asc())
    ).all()
    
    crit_scores = db.scalars(
        select(ReviewCriterionScore)
    ).all()
    crit_by_rev = defaultdict(dict)
    for cs in crit_scores:
        crit_by_rev[(cs.judge_id, cs.project_id)][cs.criterion_key] = float(cs.score)

    signatures = {}
    for r in reviews:
        c_dict = crit_by_rev.get((r.judge_id, r.project_id), {})
        c_str = json.dumps(c_dict, sort_keys=True)
        c_hash = hashlib.sha256(c_str.encode("utf-8")).hexdigest()[:16]
        signatures[f"{r.judge_id}:{r.project_id}"] = {
            "status": r.status,
            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
            "version": r.version,
            "rubric_id": r.rubric_id,
            "criteria_hash": c_hash,
        }
    return signatures


def create_results_snapshot(
    db: Session,
    event_id: str,
    creator_account: Optional[Account],
    method: str = "zscore_v1",
    parameters: Optional[dict[str, Any]] = None,
) -> EventResultsSnapshot:
    """Compute and store an immutable results snapshot with cryptographic input signatures."""
    params = parameters or {"min_sample": 3, "epsilon": 1e-6}
    min_sample = int(params.get("min_sample", 3))
    epsilon = float(params.get("epsilon", 1e-6))
    track_id = params.get("track_id")
    rubric_id = params.get("rubric_id")

    results_data = calculate_event_results(
        db=db,
        event_id=event_id,
        track_id=track_id,
        rubric_id=rubric_id,
        min_sample=min_sample,
        epsilon=epsilon,
    )

    signatures = compute_event_review_signatures(db, event_id)
    snapshot_id = f"snp_{event_id}_{int(datetime.now(timezone.utc).timestamp())}_{uuid4().hex[:8]}"

    snapshot = EventResultsSnapshot(
        id=snapshot_id,
        event_id=event_id,
        created_by_account_id=creator_account.id if creator_account else None,
        method=method,
        parameters=params,
        input_review_hashes=signatures,
        results_data=results_data,
        created_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def check_snapshot_staleness(db: Session, snapshot: EventResultsSnapshot) -> dict[str, Any]:
    """Compare stored snapshot signatures against current live database state."""
    current_signatures = compute_event_review_signatures(db, snapshot.event_id)
    stored_signatures = snapshot.input_review_hashes or {}

    stale_reasons = []
    
    # Check for added or removed reviews
    current_keys = set(current_signatures.keys())
    stored_keys = set(stored_signatures.keys())

    added_keys = current_keys - stored_keys
    removed_keys = stored_keys - current_keys

    if added_keys:
        stale_reasons.append(f"{len(added_keys)} new review(s) submitted since snapshot")
    if removed_keys:
        stale_reasons.append(f"{len(removed_keys)} review(s) removed since snapshot")

    # Check for modified reviews
    modified_count = 0
    for k in current_keys & stored_keys:
        curr = current_signatures[k]
        stored = stored_signatures[k]
        if curr.get("status") != stored.get("status") or curr.get("criteria_hash") != stored.get("criteria_hash"):
            modified_count += 1

    if modified_count > 0:
        stale_reasons.append(f"{modified_count} review(s) updated since snapshot")

    is_stale = (len(stale_reasons) > 0)
    return {
        "is_stale": is_stale,
        "stale_reasons": stale_reasons,
        "summary": "Snapshot is out of date with live database" if is_stale else "Snapshot is synchronized with live reviews",
    }
