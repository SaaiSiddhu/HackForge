import csv
import io
import math
from typing import Any, Optional
from datetime import datetime
from collections import defaultdict

from sqlalchemy import select, and_
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
from .scoring_services import calculate_event_results, evaluate_review_eligibility, calculate_raw_review_score


def sanitize_csv_cell(val: Any) -> Any:
    """Sanitize CSV cell content against CSV/formula injection (RFC 4180 safe).
    
    Prepends a single quote (') if the value starts with =, +, -, @, \\t, or \\r,
    unless the value represents a valid standalone numeric literal (e.g. negative float).
    Converts None to empty string (never fabricating zeros).
    """
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return ""
        return val

    s = str(val)
    if not s:
        return ""

    if s[0] in ("=", "+", "-", "@", "\t", "\r"):
        # Allow bare numeric values such as "-1.25"
        try:
            float(s)
            return s
        except ValueError:
            return "'" + s
    return s


def build_csv_string(headers: list[str], rows: list[list[Any]]) -> str:
    """Build an RFC 4180 compliant CSV string with CRLF line terminators and formula protection."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([sanitize_csv_cell(h) for h in headers])
    for row in rows:
        writer.writerow([sanitize_csv_cell(c) for c in row])
    return output.getvalue()


def generate_judging_results_csv(
    results_data: dict[str, Any],
    cohort_id: Optional[str] = None,
) -> str:
    """Generate judging results CSV from calculated results data or snapshot.
    
    Header:
    rank,project_id,project_title,track_id,team_id,team_name,normalized_index,normalized_rank,raw_mean,raw_rank,completed_reviews,target_reviews,coverage_status,is_provisional,rubric_id
    """
    headers = [
        "rank",
        "project_id",
        "project_title",
        "track_id",
        "team_id",
        "team_name",
        "normalized_index",
        "normalized_rank",
        "raw_mean",
        "raw_rank",
        "completed_reviews",
        "target_reviews",
        "coverage_status",
        "is_provisional",
        "rubric_id",
    ]

    cohorts = results_data.get("cohorts", [])
    if not cohorts:
        return build_csv_string(headers, [])

    # Select cohort
    selected_cohort = None
    if cohort_id:
        selected_cohort = next((c for c in cohorts if c.get("cohort_id") == cohort_id), None)
    if not selected_cohort:
        selected_cohort = cohorts[0]

    rows = []
    projects = selected_cohort.get("projects", [])
    for p in projects:
        display_rank = p.get("normalized_rank") or p.get("raw_rank") or ""
        norm_idx = f"{p['normalized_index']:.4f}" if p.get("normalized_index") is not None else ""
        norm_rank = p.get("normalized_rank") if p.get("normalized_rank") is not None else ""
        raw_m = f"{p['raw_mean']:.2f}" if p.get("raw_mean") is not None else ""
        raw_r = p.get("raw_rank") if p.get("raw_rank") is not None else ""

        rows.append([
            display_rank,
            p.get("project_id", ""),
            p.get("title", ""),
            p.get("track_id", ""),
            p.get("team_id", ""),
            p.get("team_name", ""),
            norm_idx,
            norm_rank,
            raw_m,
            raw_r,
            p.get("completed_reviews", 0),
            p.get("target_reviews", 3),
            p.get("coverage_status", ""),
            p.get("is_provisional", False),
            p.get("rubric_id", ""),
        ])

    return build_csv_string(headers, rows)


def generate_reviews_csv(db: Session, event_id: str) -> str:
    """Generate detailed reviews CSV including eligibility, raw scores, and rubric binding."""
    headers = [
        "review_id",
        "event_id",
        "judge_id",
        "judge_name",
        "judge_email",
        "project_id",
        "project_title",
        "track_id",
        "status",
        "rubric_id",
        "rubric_version",
        "raw_score",
        "is_eligible",
        "ineligibility_reason",
        "created_at",
        "updated_at",
    ]

    reviews = db.scalars(
        select(Review).where(Review.event_id == event_id).order_by(Review.project_id.asc(), Review.judge_id.asc())
    ).all()

    judges = {j.id: j for j in db.scalars(select(Judge).where(Judge.event_id == event_id)).all()}
    projects = {p.id: p for p in db.scalars(select(Project).where(Project.event_id == event_id)).all()}
    rubrics = {r.id: r for r in db.scalars(select(Rubric).where(Rubric.event_id == event_id)).all()}

    # Preload team members for conflicts
    teams = db.scalars(select(Team).where(Team.event_id == event_id)).all()
    team_members = defaultdict(set)
    for tm in teams:
        for m in db.scalars(select(TeamMembership).where(TeamMembership.team_id == tm.id)).all():
            team_members[tm.id].add(m.account_id)

    crit_rows = db.scalars(
        select(ReviewCriterionScore)
    ).all()
    crit_by_rev = defaultdict(dict)
    for cr in crit_rows:
        crit_by_rev[(cr.judge_id, cr.project_id)][cr.criterion_key] = float(cr.score)

    rows = []
    for r in reviews:
        j = judges.get(r.judge_id)
        p = projects.get(r.project_id)
        rub = rubrics.get(r.rubric_id)
        c_scores = crit_by_rev.get((r.judge_id, r.project_id), {})
        t_mems = team_members.get(p.team_id, set()) if p else set()

        is_eligible, inelig_reason = evaluate_review_eligibility(
            review=r,
            project=p,
            judge=j,
            rubric=rub,
            criteria_scores=c_scores,
            team_members=t_mems,
        )

        raw_display = ""
        if rub and c_scores and is_eligible:
            try:
                calc_raw = calculate_raw_review_score(rub, c_scores)
                raw_display = f"{calc_raw:.2f}"
            except Exception:
                raw_display = f"{r.raw_score:.2f}" if r.raw_score is not None else ""
        elif r.raw_score is not None:
            raw_display = f"{r.raw_score:.2f}"

        created_str = r.created_at.isoformat() if r.created_at else ""
        updated_str = r.updated_at.isoformat() if r.updated_at else ""

        rows.append([
            f"{r.judge_id}:{r.project_id}",
            event_id,
            r.judge_id,
            j.name if j else "",
            j.email if j else "",
            r.project_id,
            p.title if p else "",
            p.track_id if p else "",
            r.status,
            r.rubric_id or "",
            rub.version if rub else "",
            raw_display,
            is_eligible,
            inelig_reason or "",
            created_str,
            updated_str,
        ])

    return build_csv_string(headers, rows)


def generate_assignments_csv(db: Session, event_id: str) -> str:
    """Generate all judge assignments CSV with completion and review status."""
    headers = [
        "assignment_id",
        "event_id",
        "judge_id",
        "judge_name",
        "judge_email",
        "project_id",
        "project_title",
        "assigned_at",
        "review_status",
        "is_completed",
    ]

    stmt = (
        select(
            JudgeAssignment.id,
            JudgeAssignment.event_id,
            JudgeAssignment.judge_id,
            JudgeAssignment.project_id,
            JudgeAssignment.assigned_at,
            Review.status.label("review_status"),
        )
        .outerjoin(
            Review,
            and_(
                Review.judge_id == JudgeAssignment.judge_id,
                Review.project_id == JudgeAssignment.project_id,
            ),
        )
        .where(JudgeAssignment.event_id == event_id)
        .order_by(JudgeAssignment.project_id.asc(), JudgeAssignment.judge_id.asc())
    )
    rows_data = db.execute(stmt).all()

    judges = {j.id: j for j in db.scalars(select(Judge).where(Judge.event_id == event_id)).all()}
    projects = {p.id: p for p in db.scalars(select(Project).where(Project.event_id == event_id)).all()}

    rows = []
    for r in rows_data:
        j = judges.get(r.judge_id)
        p = projects.get(r.project_id)
        r_status = r.review_status or "unstarted"
        is_completed = (r_status == "finalized")
        assigned_str = r.assigned_at.isoformat() if r.assigned_at else ""

        rows.append([
            r.id,
            r.event_id,
            r.judge_id,
            j.name if j else "",
            j.email if j else "",
            r.project_id,
            p.title if p else "",
            assigned_str,
            r_status,
            is_completed,
        ])

    return build_csv_string(headers, rows)


def generate_teams_csv(db: Session, event_id: str) -> str:
    """Generate teams CSV with member lists and associated submitted project."""
    headers = [
        "team_id",
        "event_id",
        "team_name",
        "track_id",
        "captain_account_id",
        "member_count",
        "member_emails",
        "project_id",
        "project_title",
    ]

    teams = db.scalars(select(Team).where(Team.event_id == event_id).order_by(Team.id.asc())).all()
    accounts = {a.id: a for a in db.scalars(select(Account)).all()}
    projects = {p.team_id: p for p in db.scalars(select(Project).where(Project.event_id == event_id)).all()}

    rows = []
    for tm in teams:
        memberships = db.scalars(select(TeamMembership).where(TeamMembership.team_id == tm.id)).all()
        m_emails = [accounts[m.account_id].email for m in memberships if m.account_id in accounts]
        p = projects.get(tm.id)

        rows.append([
            tm.id,
            tm.event_id,
            tm.name,
            p.track_id if p else "",
            tm.captain_account_id or "",
            len(m_emails),
            "; ".join(m_emails),
            p.id if p else "",
            p.title if p else "",
        ])

    return build_csv_string(headers, rows)


def generate_projects_csv(db: Session, event_id: str) -> str:
    """Generate projects CSV with assignment and completed review counts."""
    headers = [
        "project_id",
        "event_id",
        "title",
        "tagline",
        "track_id",
        "team_id",
        "team_name",
        "status",
        "submitted_at",
        "assigned_judges_count",
        "completed_reviews_count",
    ]

    projects = db.scalars(select(Project).where(Project.event_id == event_id).order_by(Project.id.asc())).all()
    teams = {tm.id: tm for tm in db.scalars(select(Team).where(Team.event_id == event_id)).all()}

    # Pre-count assignments
    asgns = db.scalars(select(JudgeAssignment).where(JudgeAssignment.event_id == event_id)).all()
    asgn_counts = defaultdict(int)
    for a in asgns:
        asgn_counts[a.project_id] += 1

    # Pre-count completed finalized reviews
    revs = db.scalars(select(Review).where(Review.event_id == event_id, Review.status == "finalized")).all()
    rev_counts = defaultdict(int)
    for r in revs:
        rev_counts[r.project_id] += 1

    rows = []
    for p in projects:
        tm = teams.get(p.team_id)
        submitted_str = p.submitted_at.isoformat() if p.submitted_at else ""

        rows.append([
            p.id,
            p.event_id,
            p.title,
            p.tagline or "",
            p.track_id or "",
            p.team_id,
            tm.name if tm else "",
            p.status,
            submitted_str,
            asgn_counts[p.id],
            rev_counts[p.id],
        ])

    return build_csv_string(headers, rows)


def generate_judges_calibration_csv(results_data: dict[str, Any]) -> str:
    """Generate judges calibration CSV showing sample sizes, means, std devs, and usability flags."""
    headers = [
        "judge_id",
        "judge_name",
        "judge_email",
        "event_id",
        "cohort_id",
        "track_id",
        "rubric_id",
        "sample_count_N",
        "mean_score",
        "population_std_dev",
        "is_usable",
        "unusable_reason",
    ]

    event_id = results_data.get("event_id", "")
    cohorts = results_data.get("cohorts", [])

    rows = []
    for cohort in cohorts:
        c_id = cohort.get("cohort_id", "")
        t_id = cohort.get("track_id") or "all"
        r_id = cohort.get("rubric_id", "")
        for j in cohort.get("judges", []):
            mean_str = f"{j['mean_score']:.2f}" if j.get("mean_score") is not None else ""
            std_str = f"{j['population_std_dev']:.4f}" if j.get("population_std_dev") is not None else ""
            rows.append([
                j.get("judge_id", ""),
                j.get("name", ""),
                j.get("email", ""),
                event_id,
                c_id,
                t_id,
                r_id,
                j.get("sample_count_N", 0),
                mean_str,
                std_str,
                j.get("is_usable", False),
                j.get("unusable_reason") or "",
            ])

    return build_csv_string(headers, rows)
