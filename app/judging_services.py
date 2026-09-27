from datetime import datetime, timedelta, timezone
import hashlib
import logging
import math
import secrets
from typing import Any, Optional
import uuid

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
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
from .services import ConcurrencyConflictError, ensure_utc, normalize_email, record_audit_log

logger = logging.getLogger(__name__)


# --- Centralized Judge Identity & Authorization ---


def get_account_judge_for_event(db: Session, event_id: str, account_id: str) -> Optional[Judge]:
    """Retrieve the Judge record explicitly linked to an account within an event, or None.

    Strictly read-only: does NOT mutate database state and does NOT confer identity
    merely based on an email match.
    """
    return db.scalars(
        select(Judge).where(Judge.event_id == event_id, Judge.account_id == account_id)
    ).first()


def is_judge_permitted_for_track(judge: Judge, track_id: str) -> bool:
    """Check if a judge is authorized to evaluate a project in track_id.

    Policy:
    - If judge.tracks contains "*", the judge has explicit unrestricted access across all tracks.
    - If judge.tracks is non-empty, track_id must be in judge.tracks.
    - If judge.tracks is empty ([] or None), the judge has NO track permissions (e.g. revoked).
    """
    tracks = judge.tracks or []
    if "*" in tracks:
        return True
    return track_id in tracks


def is_account_event_organizer(db: Session, event_id: str, account_id: str) -> bool:
    """Check if account has organizer role for the event or is platform admin."""
    account = db.get(Account, account_id)
    if not account:
        return False
    if account.is_platform_admin:
        return True
    role = db.scalars(
        select(EventRole).where(
            EventRole.event_id == event_id,
            EventRole.account_id == account_id,
            EventRole.role == "organizer",
        )
    ).first()
    return role is not None


# --- Judge Invitations ---


def create_judge_invitation(
    db: Session,
    event_id: str,
    email: str,
    tracks: list[str],
    invited_by: Account,
) -> tuple[JudgeInvitation, str]:
    """Create a high-entropy judge invitation with hashed storage."""
    event = db.get(Event, event_id)
    if not event:
        raise ValueError(f"Event '{event_id}' not found.")

    clean_email = normalize_email(email)
    if not clean_email or "@" not in clean_email:
        raise ValueError("A valid email address is required.")

    # Validate that requested tracks belong to the event
    if tracks:
        if "*" in tracks or tracks == ["*"]:
            valid_tracks = ["*"]
        else:
            valid_tracks = list(db.scalars(
                select(Track.id).where(Track.event_id == event_id, Track.id.in_(tracks))
            ).all())
            if len(valid_tracks) != len(set(tracks)):
                raise ValueError("One or more selected tracks are invalid for this event.")
    else:
        # Explicit representation of "all tracks"
        valid_tracks = ["*"]

    # Generate high-entropy token and hash
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=14)

    invitation = JudgeInvitation(
        id=f"jinv_{uuid.uuid4().hex[:12]}",
        event_id=event_id,
        email=clean_email,
        token_hash=token_hash,
        tracks=list(valid_tracks),
        status="pending",
        invited_by_account_id=invited_by.id,
        expires_at=expires_at,
        created_at=now,
    )
    db.add(invitation)

    record_audit_log(
        db,
        event_id=event_id,
        action="judge_invitation_created",
        payload={
            "invitation_id": invitation.id,
            "email": clean_email,
            "tracks": list(valid_tracks),
        },
        account_id=invited_by.id,
        commit=False,
    )
    db.commit()
    db.refresh(invitation)
    return invitation, raw_token


def get_invitation_by_token(db: Session, raw_token: str) -> Optional[JudgeInvitation]:
    """Look up an invitation by raw token without mutating state."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return db.scalars(
        select(JudgeInvitation).where(JudgeInvitation.token_hash == token_hash)
    ).first()


def accept_judge_invitation(db: Session, raw_token: str, account: Account) -> Judge:
    """Accept an invitation, enforce email match, and link/create judge identity."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    stmt = select(JudgeInvitation).where(JudgeInvitation.token_hash == token_hash).with_for_update()
    invitation = db.scalars(stmt).first()

    if not invitation:
        raise ValueError("Invalid or unknown invitation token.")

    if invitation.status != "pending":
        raise ValueError(f"Invitation cannot be accepted because it is already {invitation.status}.")

    now = datetime.now(timezone.utc)
    expires_utc = ensure_utc(invitation.expires_at)
    if expires_utc and now > expires_utc:
        invitation.status = "expired"
        db.commit()
        raise ValueError("Invitation has expired.")

    # Strict email recipient check
    if invitation.email.lower() != account.email.lower():
        raise ValueError(
            f"This invitation was issued for '{invitation.email}', but you are signed in as '{account.email}'."
        )

    event_id = invitation.event_id

    # Check if judge record already exists for this event and account
    judge = get_account_judge_for_event(db, event_id, account.id)
    if not judge:
        # Check if an unlinked imported judge record exists matching account.email
        existing_judge_by_email = db.scalars(
            select(Judge).where(
                Judge.event_id == event_id,
                func.lower(Judge.email) == account.email.lower(),
            )
        ).first()

        if existing_judge_by_email:
            if existing_judge_by_email.account_id and existing_judge_by_email.account_id != account.id:
                raise ValueError(
                    f"Conflict: Judge record for '{account.email}' is already linked to another account."
                )
            # Explicit authorized linking within invitation acceptance transaction
            existing_judge_by_email.account_id = account.id
            judge = existing_judge_by_email
        else:
            # Create new Judge record
            judge = Judge(
                id=f"jdg_{uuid.uuid4().hex[:10]}",
                event_id=event_id,
                account_id=account.id,
                name=account.name,
                email=account.email,
                tracks=list(invitation.tracks or []),
            )
            db.add(judge)
            db.flush()

    # Track permissions merging
    inv_tracks = invitation.tracks or []
    existing_tracks = judge.tracks or []
    if "*" in inv_tracks or "*" in existing_tracks:
        judge.tracks = ["*"]
    else:
        judge.tracks = list(set(existing_tracks).union(set(inv_tracks)))
    judge.account_id = account.id

    # Sync judge_tracks table
    all_event_tracks = db.scalars(select(Track.id).where(Track.event_id == event_id)).all()
    tracks_to_sync = all_event_tracks if "*" in judge.tracks else judge.tracks
    for trk_id in tracks_to_sync:
        jt = db.scalars(
            select(JudgeTrackPermission).where(
                JudgeTrackPermission.judge_id == judge.id,
                JudgeTrackPermission.track_id == trk_id,
            )
        ).first()
        if not jt:
            db.add(JudgeTrackPermission(
                id=f"jt_{judge.id}_{trk_id}",
                judge_id=judge.id,
                track_id=trk_id,
            ))

    # Ensure event role 'judge' exists
    role = db.scalars(
        select(EventRole).where(
            EventRole.event_id == event_id,
            EventRole.account_id == account.id,
            EventRole.role == "judge",
        )
    ).first()
    if not role:
        db.add(EventRole(
            id=f"er_{event_id}_{account.id}_judge",
            event_id=event_id,
            account_id=account.id,
            role="judge",
            created_at=now,
        ))

    # Mark invitation accepted
    invitation.status = "accepted"
    invitation.accepted_by_account_id = account.id

    record_audit_log(
        db,
        event_id=event_id,
        action="judge_invitation_accepted",
        payload={
            "invitation_id": invitation.id,
            "judge_id": judge.id,
            "tracks": judge.tracks,
        },
        account_id=account.id,
        commit=False,
    )

    db.commit()
    db.refresh(judge)
    return judge


def revoke_judge_invitation(db: Session, event_id: str, invitation_id: str, actor: Account) -> JudgeInvitation:
    """Revoke a pending judge invitation."""
    invitation = db.scalars(
        select(JudgeInvitation).where(
            JudgeInvitation.id == invitation_id,
            JudgeInvitation.event_id == event_id,
        ).with_for_update()
    ).first()

    if not invitation:
        raise ValueError(f"Invitation '{invitation_id}' not found.")

    if invitation.status != "pending":
        raise ValueError(f"Cannot revoke invitation that is currently {invitation.status}.")

    invitation.status = "revoked"
    record_audit_log(
        db,
        event_id=event_id,
        action="judge_invitation_revoked",
        payload={"invitation_id": invitation.id, "email": invitation.email},
        account_id=actor.id,
        commit=False,
    )
    db.commit()
    db.refresh(invitation)
    return invitation


# --- Weighted Rubrics ---


def get_active_rubric(db: Session, event_id: str) -> Optional[Rubric]:
    """Retrieve active rubric for an event with criteria loaded in order."""
    return db.scalars(
        select(Rubric)
        .where(Rubric.event_id == event_id, Rubric.is_active == True)
        .order_by(Rubric.version.desc())
    ).first()


def validate_rubric_criteria_specs(criteria_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate rubric criteria: non-negative weights, valid score bounds, sum(weights) > 0."""
    if not criteria_specs:
        raise ValueError("Rubric must contain at least one criterion.")

    validated = []
    seen_keys = set()
    total_weight = 0.0

    for idx, c in enumerate(criteria_specs):
        key = str(c.get("key", "")).strip().lower()
        if not key:
            raise ValueError(f"Criterion at index {idx} must have a non-empty key.")
        if key in seen_keys:
            raise ValueError(f"Duplicate criterion key '{key}' is not allowed.")
        seen_keys.add(key)

        name = str(c.get("name", "")).strip() or key.replace("_", " ").title()
        description = str(c.get("description", "")).strip()

        try:
            weight = float(c.get("weight", 1.0))
            min_score = float(c.get("min_score", 1.0))
            max_score = float(c.get("max_score", 5.0))
        except (ValueError, TypeError):
            raise ValueError(f"Criterion '{key}' has non-numeric weight or score bounds.")

        if not math.isfinite(weight) or not math.isfinite(min_score) or not math.isfinite(max_score):
            raise ValueError(f"Criterion '{key}' parameters must be finite numbers.")

        if weight < 0.0:
            raise ValueError(f"Criterion '{key}' weight cannot be negative ({weight}).")

        if min_score >= max_score:
            raise ValueError(
                f"Criterion '{key}' minimum score ({min_score}) must be strictly less than maximum score ({max_score})."
            )

        total_weight += weight
        validated.append({
            "key": key,
            "name": name,
            "description": description,
            "weight": weight,
            "min_score": min_score,
            "max_score": max_score,
            "order_idx": int(c.get("order_idx", idx)),
        })

    if total_weight <= 0.0:
        raise ValueError("The sum of weights across all criteria must be strictly greater than zero.")

    return validated


def create_or_update_event_rubric(
    db: Session,
    event_id: str,
    name: str,
    criteria_specs: list[dict[str, Any]],
    actor: Account,
) -> Rubric:
    """Create or update rubric. If existing reviews already use the active rubric, a new version is created."""
    event = db.get(Event, event_id)
    if not event:
        raise ValueError(f"Event '{event_id}' not found.")

    clean_specs = validate_rubric_criteria_specs(criteria_specs)
    clean_name = name.strip() or "Evaluation Rubric"
    now = datetime.now(timezone.utc)

    active_rubric = get_active_rubric(db, event_id)

    # Check if reviews already exist using active rubric
    reviews_count = 0
    if active_rubric:
        reviews_count = db.scalar(
            select(func.count(Review.judge_id)).where(Review.rubric_id == active_rubric.id)
        ) or 0

    if active_rubric and reviews_count > 0:
        # Freeze active rubric and create new version
        active_rubric.is_active = False
        new_version = active_rubric.version + 1
        new_rubric_id = f"rb_{event_id}_v{new_version}"

        new_rubric = Rubric(
            id=new_rubric_id,
            event_id=event_id,
            version=new_version,
            name=clean_name,
            is_active=True,
            created_at=now,
        )
        db.add(new_rubric)
        db.flush()

        for s in clean_specs:
            crit = RubricCriterion(
                id=f"rc_{new_rubric.id}_{s['key']}",
                rubric_id=new_rubric.id,
                key=s["key"],
                name=s["name"],
                description=s["description"],
                weight=s["weight"],
                min_score=s["min_score"],
                max_score=s["max_score"],
                order_idx=s["order_idx"],
            )
            db.add(crit)

        record_audit_log(
            db,
            event_id=event_id,
            action="rubric_version_created",
            payload={"rubric_id": new_rubric.id, "version": new_version, "criteria_count": len(clean_specs)},
            account_id=actor.id,
            commit=False,
        )
        db.commit()
        db.refresh(new_rubric)
        return new_rubric

    elif active_rubric:
        # No reviews yet: update existing rubric in place
        active_rubric.name = clean_name
        # Delete existing criteria
        for c in list(active_rubric.criteria):
            db.delete(c)
        db.flush()

        for s in clean_specs:
            crit = RubricCriterion(
                id=f"rc_{active_rubric.id}_{s['key']}",
                rubric_id=active_rubric.id,
                key=s["key"],
                name=s["name"],
                description=s["description"],
                weight=s["weight"],
                min_score=s["min_score"],
                max_score=s["max_score"],
                order_idx=s["order_idx"],
            )
            db.add(crit)

        record_audit_log(
            db,
            event_id=event_id,
            action="rubric_updated",
            payload={"rubric_id": active_rubric.id, "version": active_rubric.version, "criteria_count": len(clean_specs)},
            account_id=actor.id,
            commit=False,
        )
        db.commit()
        db.refresh(active_rubric)
        return active_rubric

    else:
        # First rubric creation
        rubric_id = f"rb_{event_id}_v1"
        rubric = Rubric(
            id=rubric_id,
            event_id=event_id,
            version=1,
            name=clean_name,
            is_active=True,
            created_at=now,
        )
        db.add(rubric)
        db.flush()

        for s in clean_specs:
            crit = RubricCriterion(
                id=f"rc_{rubric.id}_{s['key']}",
                rubric_id=rubric.id,
                key=s["key"],
                name=s["name"],
                description=s["description"],
                weight=s["weight"],
                min_score=s["min_score"],
                max_score=s["max_score"],
                order_idx=s["order_idx"],
            )
            db.add(crit)

        record_audit_log(
            db,
            event_id=event_id,
            action="rubric_created",
            payload={"rubric_id": rubric.id, "version": 1, "criteria_count": len(clean_specs)},
            account_id=actor.id,
            commit=False,
        )
        db.commit()
        db.refresh(rubric)
        return rubric


def calculate_raw_score(rubric: Rubric, criterion_scores: dict[str, float]) -> Optional[float]:
    """Calculate raw review total: 100 * sum(weight * (score - min)/(max - min)) / sum(weights).

    Rounded to 2 decimal places. Returns None if rubric has no weights or scores are empty.
    """
    if not rubric or not rubric.criteria or not criterion_scores:
        return None

    tot_weights = 0.0
    tot_weighted_contrib = 0.0

    crit_by_key = {c.key: c for c in rubric.criteria}

    for key, score in criterion_scores.items():
        crit = crit_by_key.get(key)
        if not crit:
            continue
        try:
            val = float(score)
        except (ValueError, TypeError):
            continue

        range_span = crit.max_score - crit.min_score
        norm_contrib = (val - crit.min_score) / range_span if range_span > 0 else 1.0
        tot_weighted_contrib += crit.weight * norm_contrib
        tot_weights += crit.weight

    if tot_weights <= 0.0:
        return None

    raw_total = 100.0 * (tot_weighted_contrib / tot_weights)
    return round(raw_total, 2)


# --- Project Assignments ---


def assign_judge_to_project(
    db: Session,
    event_id: str,
    judge_id: str,
    project_id: str,
    actor: Account,
) -> JudgeAssignment:
    """Manually assign a judge to a project with eligibility, track, and conflict verification."""
    judge = db.get(Judge, judge_id)
    if not judge or judge.event_id != event_id:
        raise ValueError(f"Judge '{judge_id}' does not belong to event '{event_id}'.")

    project = db.get(Project, project_id)
    if not project or project.event_id != event_id:
        raise ValueError(f"Project '{project_id}' does not belong to event '{event_id}'.")

    if project.status != "submitted":
        raise ValueError("Cannot assign judges to draft (unsubmitted) projects.")

    # Track permission check
    if not is_judge_permitted_for_track(judge, project.track_id):
        raise ValueError(f"Judge '{judge.name}' is not permitted to evaluate track '{project.track_id}'.")

    # Own team conflict exclusion
    if judge.account_id:
        team_member = db.scalars(
            select(TeamMembership).where(
                TeamMembership.team_id == project.team_id,
                TeamMembership.account_id == judge.account_id,
            )
        ).first()
        if team_member:
            raise ValueError(f"Conflict of interest: Judge '{judge.name}' belongs to project's team.")

    # Duplicate check
    existing = db.scalars(
        select(JudgeAssignment).where(
            JudgeAssignment.judge_id == judge_id,
            JudgeAssignment.project_id == project_id,
        )
    ).first()
    if existing:
        return existing

    now = datetime.now(timezone.utc)
    assignment = JudgeAssignment(
        id=f"asgn_{judge_id}_{project_id}",
        event_id=event_id,
        judge_id=judge_id,
        project_id=project_id,
        assigned_at=now,
    )
    db.add(assignment)

    record_audit_log(
        db,
        event_id=event_id,
        action="judge_assigned",
        payload={"judge_id": judge_id, "project_id": project_id},
        account_id=actor.id,
        commit=False,
    )
    db.commit()
    db.refresh(assignment)
    return assignment


def unassign_judge_from_project(
    db: Session,
    event_id: str,
    judge_id: str,
    project_id: str,
    actor: Account,
) -> None:
    """Remove a judge assignment. Does not delete historical completed reviews."""
    assignment = db.scalars(
        select(JudgeAssignment).where(
            JudgeAssignment.event_id == event_id,
            JudgeAssignment.judge_id == judge_id,
            JudgeAssignment.project_id == project_id,
        )
    ).first()
    if not assignment:
        return

    db.delete(assignment)
    record_audit_log(
        db,
        event_id=event_id,
        action="judge_unassigned",
        payload={"judge_id": judge_id, "project_id": project_id},
        account_id=actor.id,
        commit=False,
    )
    db.commit()


def run_batch_assignment(
    db: Session,
    event_id: str,
    target_reviews_per_project: int,
    actor: Account,
) -> dict[str, Any]:
    """Deterministically assign judges to submitted projects, balancing workload and respecting tracks/conflicts."""
    if target_reviews_per_project < 1:
        raise ValueError("Target reviews per project must be at least 1.")

    # 1. Fetch submitted projects only
    projects = db.scalars(
        select(Project)
        .where(Project.event_id == event_id, Project.status == "submitted")
        .order_by(Project.id.asc())
    ).all()

    # 2. Fetch judges for event
    judges = db.scalars(
        select(Judge).where(Judge.event_id == event_id).order_by(Judge.id.asc())
    ).all()

    # 3. Preload all existing assignments for event
    existing_assignments = db.scalars(
        select(JudgeAssignment).where(JudgeAssignment.event_id == event_id)
    ).all()

    # Map project_id -> set of assigned judge_ids
    proj_assigned = {p.id: set() for p in projects}
    # Map judge_id -> total assignment count
    judge_workload = {j.id: 0 for j in judges}

    for asgn in existing_assignments:
        if asgn.project_id in proj_assigned:
            proj_assigned[asgn.project_id].add(asgn.judge_id)
        if asgn.judge_id in judge_workload:
            judge_workload[asgn.judge_id] += 1

    # Map team_id -> set of account_ids for conflict exclusion
    team_members = {}
    teams = db.scalars(select(Team).where(Team.event_id == event_id)).all()
    for tm in teams:
        team_members[tm.id] = set()
        memberships = db.scalars(select(TeamMembership).where(TeamMembership.team_id == tm.id)).all()
        for m in memberships:
            team_members[tm.id].add(m.account_id)

    new_assignments_count = 0
    now = datetime.now(timezone.utc)
    under_covered_projects = []

    # Process projects deterministically
    for project in projects:
        current_assigned = proj_assigned[project.id]
        needed = target_reviews_per_project - len(current_assigned)
        if needed <= 0:
            continue

        # Find eligible judges
        eligible = []
        for judge in judges:
            if judge.id in current_assigned:
                continue

            # Track permission
            if not is_judge_permitted_for_track(judge, project.track_id):
                continue

            # Team conflict
            if judge.account_id and judge.account_id in team_members.get(project.team_id, set()):
                continue

            eligible.append(judge)

        # Sort eligible judges by workload ascending, then deterministic tie-breaker judge.id asc
        eligible.sort(key=lambda j: (judge_workload[j.id], j.id))

        assigned_here = 0
        for judge in eligible[:needed]:
            asgn = JudgeAssignment(
                id=f"asgn_{judge.id}_{project.id}",
                event_id=event_id,
                judge_id=judge.id,
                project_id=project.id,
                assigned_at=now,
            )
            db.add(asgn)
            proj_assigned[project.id].add(judge.id)
            judge_workload[judge.id] += 1
            new_assignments_count += 1
            assigned_here += 1

        if len(proj_assigned[project.id]) < target_reviews_per_project:
            under_covered_projects.append({
                "project_id": project.id,
                "title": project.title,
                "current_count": len(proj_assigned[project.id]),
                "target": target_reviews_per_project,
            })

    record_audit_log(
        db,
        event_id=event_id,
        action="batch_assignment_run",
        payload={
            "target_reviews_per_project": target_reviews_per_project,
            "new_assignments_count": new_assignments_count,
            "uncovered_projects_count": len(under_covered_projects),
        },
        account_id=actor.id,
        commit=False,
    )
    db.commit()

    return {
        "new_assignments_count": new_assignments_count,
        "total_projects": len(projects),
        "total_judges": len(judges),
        "uncovered_projects": under_covered_projects,
    }


# --- Judge Scoring & Reviews ---


def get_judge_assigned_projects(
    db: Session,
    event_id: str,
    judge: Judge,
    track_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Retrieve assigned projects for a judge, with track/status filtering and review status."""
    stmt = (
        select(JudgeAssignment, Project)
        .join(Project, JudgeAssignment.project_id == Project.id)
        .where(JudgeAssignment.event_id == event_id, JudgeAssignment.judge_id == judge.id)
        .order_by(Project.title.asc())
    )
    if track_filter:
        stmt = stmt.where(Project.track_id == track_filter)

    results = db.execute(stmt).all()
    output = []

    for asgn, proj in results:
        # Track permission check
        if not is_judge_permitted_for_track(judge, proj.track_id):
            continue

        # Own-team conflict check
        if judge.account_id and db.scalars(
            select(TeamMembership).where(
                TeamMembership.team_id == proj.team_id,
                TeamMembership.account_id == judge.account_id,
            )
        ).first() is not None:
            continue

        # Load review if present
        rev = db.scalars(
            select(Review).where(Review.judge_id == judge.id, Review.project_id == proj.id)
        ).first()

        rev_status = rev.status if rev else "unstarted"
        if status_filter:
            if status_filter == "completed" and rev_status != "finalized":
                continue
            if status_filter == "in_progress" and rev_status != "draft":
                continue
            if status_filter == "unstarted" and rev_status != "unstarted":
                continue

        output.append({
            "project_id": proj.id,
            "title": proj.title,
            "tagline": proj.tagline,
            "summary": proj.summary,
            "track_id": proj.track_id,
            "track_name": proj.track.name if proj.track else proj.track_id,
            "team_id": proj.team_id,
            "team_name": proj.team.name if proj.team else proj.team_id,
            "assigned_at": asgn.assigned_at,
            "review_status": rev_status,
            "raw_score": rev.raw_score if rev else None,
            "review_version": rev.version if rev else 0,
        })

    return output


def get_project_for_judge_evaluation(
    db: Session,
    event_id: str,
    judge: Judge,
    project_id: str,
) -> tuple[Project, Optional[Review], Rubric]:
    """Centralized isolation check: ensures judge is assigned to project, permitted for track, and not in conflict."""
    project = db.get(Project, project_id)
    if not project or project.event_id != event_id:
        raise ValueError(f"Project '{project_id}' not found for event '{event_id}'.")

    if project.status != "submitted":
        raise ValueError("Cannot evaluate unsubmitted project.")

    # 1. Assignment check
    is_assigned = db.scalars(
        select(JudgeAssignment).where(
            JudgeAssignment.judge_id == judge.id,
            JudgeAssignment.project_id == project_id,
        )
    ).first() is not None
    if not is_assigned:
        raise PermissionError(f"Forbidden: Judge '{judge.id}' is not assigned to project '{project_id}'.")

    # 2. Track permission check
    if not is_judge_permitted_for_track(judge, project.track_id):
        raise PermissionError(f"Forbidden: Judge track permissions do not cover track '{project.track_id}'.")

    # 3. Own-team conflict check
    if judge.account_id:
        is_teammate = db.scalars(
            select(TeamMembership).where(
                TeamMembership.team_id == project.team_id,
                TeamMembership.account_id == judge.account_id,
            )
        ).first() is not None
        if is_teammate:
            raise PermissionError("Forbidden: Conflict of interest (judge is a member of the project team).")

    # 4. Review & bound rubric
    review = db.scalars(
        select(Review).where(Review.judge_id == judge.id, Review.project_id == project_id)
    ).first()

    if review and review.rubric_id:
        rubric = db.get(Rubric, review.rubric_id)
        if not rubric:
            rubric = get_active_rubric(db, event_id)
    else:
        rubric = get_active_rubric(db, event_id)

    if not rubric:
        raise ValueError(f"No active evaluation rubric configured for event '{event_id}'.")

    return project, review, rubric


def _record_conflict_safely(
    db: Session,
    event_id: str,
    actor_id: Optional[str],
    payload: dict[str, Any],
) -> None:
    """Record a review.conflict audit log entry using a separate transaction without committing caller session."""
    if not actor_id:
        return
    try:
        bind = db.get_bind()
        with Session(bind) as audit_db:
            audit_log = EventAuditLog(
                id=f"log_{uuid.uuid4().hex[:12]}",
                event_id=event_id,
                account_id=actor_id,
                action="review.conflict",
                payload=payload,
                created_at=datetime.now(timezone.utc),
            )
            audit_db.add(audit_log)
            audit_db.commit()
    except Exception as e:
        logger.warning(f"Could not record conflict audit log: {e}")


def _validate_criteria_scores(
    rubric: Rubric,
    criteria_inputs: dict[str, Any],
    is_final: bool,
) -> dict[str, float]:
    """Validate criteria scores against the specified rubric."""
    rubric_criteria_by_key = {c.key: c for c in rubric.criteria}
    cleaned_scores = {}

    if is_final:
        for crit in rubric.criteria:
            raw_val = criteria_inputs.get(crit.key)
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                raise ValueError(f"Criterion '{crit.name}' must be scored before final submission.")
            try:
                score_f = float(raw_val)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid numeric score '{raw_val}' for criterion '{crit.name}'.")

            if not math.isfinite(score_f):
                raise ValueError(f"Score for criterion '{crit.name}' must be a finite number.")

            if score_f < crit.min_score or score_f > crit.max_score:
                raise ValueError(
                    f"Score for criterion '{crit.name}' ({score_f}) must be between {crit.min_score} and {crit.max_score}."
                )
            cleaned_scores[crit.key] = score_f

        for k in criteria_inputs:
            if k not in rubric_criteria_by_key:
                raise ValueError(f"Unknown criterion key '{k}' not found in active rubric.")

    else:
        for k, v in criteria_inputs.items():
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            if k not in rubric_criteria_by_key:
                raise ValueError(f"Unknown criterion key '{k}' not found in active rubric.")
            crit = rubric_criteria_by_key[k]
            try:
                score_f = float(v)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid numeric score '{v}' for criterion '{crit.name}'.")

            if not math.isfinite(score_f):
                raise ValueError(f"Score for criterion '{crit.name}' must be a finite number.")

            if score_f < crit.min_score or score_f > crit.max_score:
                raise ValueError(
                    f"Score for criterion '{crit.name}' ({score_f}) must be between {crit.min_score} and {crit.max_score}."
                )
            cleaned_scores[k] = score_f

    return cleaned_scores


def save_judge_evaluation(
    db: Session,
    event_id: str,
    judge: Judge,
    project_id: str,
    criteria_inputs: dict[str, Any],
    comment: str,
    is_final: bool,
    expected_version: Optional[int],
    actor: Account,
    form_rubric_id: Optional[str] = None,
) -> Review:
    """Save draft or finalize review with OCC row locking, full rubric validation, and atomic audit logging."""
    project = db.get(Project, project_id)
    if not project or project.event_id != event_id:
        raise ValueError(f"Project '{project_id}' not found for event '{event_id}'.")

    if project.status != "submitted":
        raise ValueError("Cannot evaluate unsubmitted project.")

    # Assignment check with parent row lock to coordinate simultaneous creation
    asgn = db.scalars(
        select(JudgeAssignment)
        .where(
            JudgeAssignment.event_id == event_id,
            JudgeAssignment.judge_id == judge.id,
            JudgeAssignment.project_id == project_id,
        )
        .with_for_update()
    ).first()
    if not asgn:
        raise PermissionError(f"Forbidden: Judge '{judge.id}' is not assigned to project '{project_id}'.")

    # Track permission check
    if not is_judge_permitted_for_track(judge, project.track_id):
        raise PermissionError(f"Forbidden: Judge track permissions do not cover track '{project.track_id}'.")

    # Own-team conflict check
    if judge.account_id:
        is_teammate = db.scalars(
            select(TeamMembership).where(
                TeamMembership.team_id == project.team_id,
                TeamMembership.account_id == judge.account_id,
            )
        ).first() is not None
        if is_teammate:
            raise PermissionError("Forbidden: Conflict of interest (judge is a member of the project team).")

    # Acquire lock on Review row if it exists, refreshing from DB
    stmt = (
        select(Review)
        .where(Review.judge_id == judge.id, Review.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_review = db.scalars(stmt).first()
    if locked_review:
        db.refresh(locked_review)

    now = datetime.now(timezone.utc)
    target_status = "finalized" if is_final else "draft"

    if locked_review:
        # Require expected_version for edits
        if expected_version is None:
            _record_conflict_safely(db, event_id, actor.id if actor else None, {
                "review_id": f"{judge.id}:{project_id}",
                "judge_id": judge.id,
                "project_id": project_id,
                "expected_version": None,
                "current_version": locked_review.version,
                "reason": "missing_expected_version",
            })
            db.rollback()
            raise ConcurrencyConflictError("Conflict: expected_version is required when modifying an existing review.")

        if locked_review.version != expected_version:
            _record_conflict_safely(db, event_id, actor.id if actor else None, {
                "review_id": f"{judge.id}:{project_id}",
                "judge_id": judge.id,
                "project_id": project_id,
                "expected_version": expected_version,
                "current_version": locked_review.version,
                "reason": "stale_version",
            })
            db.rollback()
            raise ConcurrencyConflictError(
                f"Conflict: Review has been modified (current version {locked_review.version}, expected {expected_version}). Please reload."
            )

        # Rubric lifecycle: Existing review is bound to its original rubric version!
        if locked_review.rubric_id:
            rubric = db.get(Rubric, locked_review.rubric_id)
            if not rubric:
                rubric = get_active_rubric(db, event_id)
        else:
            rubric = get_active_rubric(db, event_id)

        if not rubric:
            raise ValueError(f"No evaluation rubric configured for event '{event_id}'.")

        # Stale rubric check
        if form_rubric_id and rubric and form_rubric_id != rubric.id:
            raise ValueError(
                f"Rubric conflict: Review is bound to rubric '{rubric.id}', but form submitted '{form_rubric_id}'. Please reload."
            )

        cleaned_scores = _validate_criteria_scores(rubric, criteria_inputs, is_final)
        raw_score = calculate_raw_score(rubric, cleaned_scores)

        locked_review.criteria = cleaned_scores
        locked_review.comment = comment.strip()
        locked_review.status = target_status
        locked_review.raw_score = raw_score
        locked_review.rubric_id = rubric.id  # Preserve bound rubric
        locked_review.version = expected_version + 1
        locked_review.updated_at = now
        review = locked_review

    else:
        # New review creation: expected_version must be None or 0
        if expected_version not in (None, 0):
            _record_conflict_safely(db, event_id, actor.id if actor else None, {
                "review_id": f"{judge.id}:{project_id}",
                "judge_id": judge.id,
                "project_id": project_id,
                "expected_version": expected_version,
                "current_version": 0,
                "reason": "unexpected_version_for_new",
            })
            db.rollback()
            raise ConcurrencyConflictError("Conflict: Cannot specify expected_version for a new review.")

        rubric = get_active_rubric(db, event_id)
        if not rubric:
            raise ValueError(f"No active evaluation rubric configured for event '{event_id}'.")

        if form_rubric_id and form_rubric_id != rubric.id:
            raise ValueError(
                f"Rubric conflict: Active rubric is '{rubric.id}', but form submitted '{form_rubric_id}'. Please reload."
            )

        cleaned_scores = _validate_criteria_scores(rubric, criteria_inputs, is_final)
        raw_score = calculate_raw_score(rubric, cleaned_scores)

        review = Review(
            judge_id=judge.id,
            project_id=project_id,
            event_id=event_id,
            rubric_id=rubric.id,
            criteria=cleaned_scores,
            comment=comment.strip(),
            status=target_status,
            raw_score=raw_score,
            version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(review)

    try:
        db.flush()
    except IntegrityError:
        # Simultaneous creation conflict caught!
        db.rollback()
        _record_conflict_safely(db, event_id, actor.id if actor else None, {
            "review_id": f"{judge.id}:{project_id}",
            "judge_id": judge.id,
            "project_id": project_id,
            "expected_version": expected_version,
            "reason": "simultaneous_creation_integrity_error",
        })
        raise ConcurrencyConflictError(
            "Conflict: A review for this project was already created by another session. Please reload."
        )

    # Sync review_criterion_scores
    rubric_criteria_by_key = {c.key: c for c in rubric.criteria}
    for k, val in cleaned_scores.items():
        crit_obj = rubric_criteria_by_key.get(k)
        rcs_id = f"rcs_{judge.id}_{project_id}_{k}"
        rcs = db.scalars(
            select(ReviewCriterionScore).where(
                ReviewCriterionScore.judge_id == judge.id,
                ReviewCriterionScore.project_id == project_id,
                ReviewCriterionScore.criterion_key == k,
            )
        ).first()
        if rcs:
            rcs.score = val
            rcs.criterion_id = crit_obj.id if crit_obj else None
        else:
            db.add(ReviewCriterionScore(
                id=rcs_id,
                judge_id=judge.id,
                project_id=project_id,
                criterion_id=crit_obj.id if crit_obj else None,
                criterion_key=k,
                score=val,
            ))

    # Audit log
    action_name = "review_finalized" if is_final else "review_saved_draft"
    record_audit_log(
        db,
        event_id=event_id,
        action=action_name,
        payload={
            "judge_id": judge.id,
            "project_id": project_id,
            "raw_score": raw_score,
            "version": review.version,
            "status": review.status,
            "rubric_id": rubric.id,
        },
        account_id=actor.id if actor else None,
        commit=False,
    )

    db.commit()
    db.refresh(review)
    return review


# --- Organizer Progress & Scores API ---


def get_organizer_judging_progress(db: Session, event_id: str) -> dict[str, Any]:
    """Compute organizer metrics strictly based on current assignments and their review states."""
    # 1. Query all current assignments outer-joined to Review
    stmt = (
        select(
            JudgeAssignment.id,
            JudgeAssignment.judge_id,
            JudgeAssignment.project_id,
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
    )
    assignment_rows = db.execute(stmt).all()

    total_assignments = len(assignment_rows)
    finalized_count = sum(1 for r in assignment_rows if r.review_status == "finalized")
    draft_count = sum(1 for r in assignment_rows if r.review_status == "draft")
    unstarted_count = sum(1 for r in assignment_rows if r.review_status not in ("finalized", "draft"))

    # 2. Submitted projects progress
    projects = db.scalars(
        select(Project).where(Project.event_id == event_id, Project.status == "submitted")
    ).all()

    project_progress = []
    projects_under_covered = []
    for p in projects:
        p_rows = [r for r in assignment_rows if r.project_id == p.id]
        p_assigned = len(p_rows)
        p_finalized = sum(1 for r in p_rows if r.review_status == "finalized")
        p_draft = sum(1 for r in p_rows if r.review_status == "draft")

        item = {
            "project_id": p.id,
            "title": p.title,
            "track_id": p.track_id,
            "assigned_count": p_assigned,
            "finalized_count": p_finalized,
            "draft_count": p_draft,
        }
        project_progress.append(item)
        if p_finalized < 2:  # Below baseline coverage threshold
            projects_under_covered.append(item)

    # 3. Judges with outstanding work
    judges = db.scalars(select(Judge).where(Judge.event_id == event_id)).all()
    judge_progress = []
    for j in judges:
        j_rows = [r for r in assignment_rows if r.judge_id == j.id]
        j_assigned = len(j_rows)
        j_finalized = sum(1 for r in j_rows if r.review_status == "finalized")
        if j_assigned > j_finalized:
            judge_progress.append({
                "judge_id": j.id,
                "name": j.name,
                "email": j.email,
                "assigned": j_assigned,
                "completed": j_finalized,
                "outstanding": j_assigned - j_finalized,
            })

    # 4. Total historical reviews (including any whose assignments were removed)
    total_historical_reviews = db.scalar(
        select(func.count(Review.judge_id)).where(Review.event_id == event_id)
    ) or 0
    historical_unassigned_reviews = max(0, total_historical_reviews - (finalized_count + draft_count))

    return {
        "event_id": event_id,
        "total_assignments": total_assignments,
        "finalized_reviews": finalized_count,
        "draft_reviews": draft_count,
        "unstarted_assignments": unstarted_count,
        "total_projects": len(projects),
        "total_judges": len(judges),
        "projects_count": len(projects),
        "projects_under_covered": len(projects_under_covered),
        "under_covered_projects": projects_under_covered,
        "under_covered_project_ids": [p["project_id"] for p in projects_under_covered],
        "judges_outstanding": judge_progress,
        "judges_with_outstanding_work": len(judge_progress),
        "project_progress": project_progress,
        "total_historical_reviews": total_historical_reviews,
        "historical_unassigned_reviews": historical_unassigned_reviews,
    }


def get_judge_scores_resource(
    db: Session,
    event_id: str,
    judge_id: str,
    acting_account: Optional[Account],
) -> dict[str, Any]:
    """Return meaningful review scores for a judge, enforcing backend isolation.

    Accessible to:
    - The judge themselves (account linked to this judge_id).
    - An event organizer for event_id (ONLY IF NOT active judge in this event).
    - Platform admin.
    Any other request is rejected with 401 or 403.
    """
    if not acting_account:
        raise PermissionError("Authentication required.")

    target_judge = db.get(Judge, judge_id)
    if not target_judge or target_judge.event_id != event_id:
        raise ValueError(f"Judge '{judge_id}' not found for event '{event_id}'.")

    # Determine acting judge identity in this event
    acting_judge = get_account_judge_for_event(db, event_id, acting_account.id)

    # Judge Isolation Rule:
    # If the user is an active judge in this event, they can ONLY view their OWN scores.
    # Holding an organizer role in addition to a judge role does NOT grant peer score visibility.
    if acting_judge:
        if acting_judge.id != judge_id:
            raise PermissionError(f"Forbidden: You cannot view peer judge '{judge_id}' scores.")
    else:
        # User is not a judge in this event. Must be organizer or platform admin.
        is_org = is_account_event_organizer(db, event_id, acting_account.id)
        if not is_org and not acting_account.is_platform_admin:
            raise PermissionError("Forbidden: You are not authorized to view judge score records.")

    # Fetch reviews
    reviews = db.scalars(
        select(Review)
        .where(Review.event_id == event_id, Review.judge_id == judge_id)
        .order_by(Review.project_id.asc())
    ).all()

    scores_list = []
    for r in reviews:
        scores_list.append({
            "project_id": r.project_id,
            "project_title": r.project.title if r.project else r.project_id,
            "criteria": r.criteria,
            "raw_score": r.raw_score,
            "comment": r.comment,
            "status": r.status,
            "version": r.version,
        })

    return {
        "event_id": event_id,
        "judge_id": target_judge.id,
        "judge_name": target_judge.name,
        "tracks": target_judge.tracks,
        "scores_count": len(scores_list),
        "scores": scores_list,
    }
