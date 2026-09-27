import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .auth import hash_password, verify_password
from .config import get_settings
from .models import (
    Account,
    Event,
    EventAuditLog,
    EventQuestion,
    EventRole,
    Project,
    Team,
    TeamInvitation,
    TeamMembership,
    Track,
)

logger = logging.getLogger("hackforge.services")

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is timezone-aware normalized to UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ConcurrencyConflictError(ValueError):
    """Raised when an update encounters an optimistic concurrency version mismatch."""
    pass


# --- Image Upload Service ---


def validate_and_save_image(
    file_bytes: bytes,
    original_filename: str = "",
    project_id: str = "general",
) -> str:
    """Validate image with Pillow, enforce max 4096px bounds, re-encode cleanly, and persist."""
    settings = get_settings()
    max_size = 5 * 1024 * 1024  # 5 MB
    if len(file_bytes) > max_size:
        raise ValueError("File exceeds maximum allowed size of 5 MB.")
    if len(file_bytes) < 8:
        raise ValueError("Invalid file content: file too small.")

    import io
    from PIL import Image

    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            img.verify()
    except Exception:
        raise ValueError("Unsupported or invalid image format: corrupted or unreadable image.")

    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            fmt = (img.format or "").upper()
            if fmt not in ("PNG", "JPEG", "JPG", "GIF", "WEBP"):
                raise ValueError(f"Unsupported image format: '{fmt}'. Supported: PNG, JPEG, GIF, WebP.")

            width, height = img.size
            if width > 4096 or height > 4096:
                raise ValueError(f"Image dimensions ({width}x{height}) exceed maximum allowed 4096x4096 pixels.")

            out = io.BytesIO()
            if fmt in ("JPEG", "JPG"):
                rgb_img = img.convert("RGB")
                rgb_img.save(out, format="JPEG", quality=85, optimize=True)
                ext = ".jpg"
            elif fmt == "PNG":
                img.save(out, format="PNG", optimize=True)
                ext = ".png"
            elif fmt == "WEBP":
                img.save(out, format="WEBP")
                ext = ".webp"
            elif fmt == "GIF":
                img.save(out, format="GIF")
                ext = ".gif"
            else:
                img.save(out, format=fmt)
                ext = f".{fmt.lower()}"

            clean_bytes = out.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Image processing error: {exc}")

    safe_project_id = project_id or "general"
    filename = f"{uuid.uuid4().hex}{ext}"
    target_dir = settings.upload_dir / safe_project_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    target_path.write_bytes(clean_bytes)

    return f"/projects/{safe_project_id}/media/{filename}"


# --- Account Service ---


def normalize_email(email: str) -> str:
    """Normalize email string to lowercase trimmed."""
    if not email:
        raise ValueError("Email cannot be empty.")
    clean = email.strip().lower()
    if not EMAIL_REGEX.match(clean):
        raise ValueError(f"Invalid email address format: '{clean}'")
    return clean


def create_account(
    db: Session,
    email: str,
    password: str,
    name: str,
    is_platform_admin: bool = False,
    account_id: Optional[str] = None,
) -> Account:
    """Create a new account with Argon2id password hashing."""
    clean_email = normalize_email(email)
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Name cannot be empty.")

    # Check for existing email
    existing = db.scalars(select(Account).where(Account.email == clean_email)).first()
    if existing:
        raise ValueError(f"Account with email '{clean_email}' already exists.")

    hashed_pw = hash_password(password)
    aid = account_id or f"acc_{uuid.uuid4().hex[:12]}"

    account = Account(
        id=aid,
        email=clean_email,
        password_hash=hashed_pw,
        name=clean_name,
        is_platform_admin=is_platform_admin,
        created_at=datetime.now(timezone.utc),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def authenticate_account(db: Session, email: str, password: str) -> Optional[Account]:
    """Verify credentials and return account if valid."""
    try:
        clean_email = normalize_email(email)
    except ValueError:
        return None

    account = db.scalars(select(Account).where(Account.email == clean_email)).first()
    if not account:
        return None

    if verify_password(password, account.password_hash):
        return account
    return None


# --- Audit Logging ---


def record_audit_log(
    db: Session,
    event_id: str,
    action: str,
    payload: dict[str, Any],
    account_id: Optional[str] = None,
    commit: bool = True,
) -> EventAuditLog:
    """Record immutable event audit trail entry."""
    log_entry = EventAuditLog(
        id=f"log_{uuid.uuid4().hex[:12]}",
        event_id=event_id,
        account_id=account_id,
        action=action,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    db.add(log_entry)
    if commit:
        db.commit()
        db.refresh(log_entry)
    return log_entry


# --- Event Management Service ---


def create_event(
    db: Session,
    event_id: str,
    name: str,
    submissions_close: datetime,
    description: str = "",
    registration_open: Optional[datetime] = None,
    registration_close: Optional[datetime] = None,
    submissions_open: Optional[datetime] = None,
    min_team_size: int = 1,
    max_team_size: int = 5,
    tracks: Optional[list] = None,
    prizes: Optional[list] = None,
    questions: Optional[list] = None,
    creator_account_id: Optional[str] = None,
) -> Event:
    """Create a new hackathon event with date ordering, tracks, and questions."""
    if not event_id or not event_id.strip():
        raise ValueError("Event ID cannot be empty.")
    if not name or not name.strip():
        raise ValueError("Event name cannot be empty.")

    clean_id = event_id.strip()
    if db.get(Event, clean_id):
        raise ValueError(f"Event with ID '{clean_id}' already exists.")

    if min_team_size < 1:
        raise ValueError("Minimum team size must be at least 1.")
    if max_team_size < min_team_size:
        raise ValueError("Maximum team size cannot be smaller than minimum team size.")

    # Validate date chronological ordering
    reg_open = ensure_utc(registration_open)
    reg_close = ensure_utc(registration_close)
    if reg_open and reg_close and reg_open > reg_close:
        raise ValueError("Registration open date must be before or equal to registration close date.")

    sub_open = ensure_utc(submissions_open)
    sub_close = ensure_utc(submissions_close)
    if not sub_close:
        raise ValueError("Submissions close date is required.")
    if sub_open and sub_close and sub_open > sub_close:
        raise ValueError("Submissions open date must be before or equal to submissions close date.")

    event = Event(
        id=clean_id,
        name=name.strip(),
        description=description.strip() if description else "",
        registration_open=reg_open,
        registration_close=reg_close,
        submissions_open=sub_open,
        submissions_close=sub_close,
        min_team_size=min_team_size,
        max_team_size=max_team_size,
        prizes=prizes or [],
        created_by_account_id=creator_account_id,
    )
    db.add(event)

    # Add tracks
    if tracks:
        for t in tracks:
            t_id = t.get("id") or f"trk_{uuid.uuid4().hex[:8]}" if isinstance(t, dict) else f"trk_{uuid.uuid4().hex[:8]}"
            t_name = t.get("name") if isinstance(t, dict) else str(t)
            if t_name and t_name.strip():
                db.add(Track(id=t_id, event_id=clean_id, name=t_name.strip()))

    # Add custom questions
    if questions:
        for idx, q in enumerate(questions):
            q_id = q.get("id") or f"qst_{uuid.uuid4().hex[:8]}"
            q_label = q.get("label", "")
            q_type = q.get("question_type", "text")
            q_req = bool(q.get("required", False))
            q_opts = q.get("options", [])
            if q_label and q_label.strip():
                db.add(EventQuestion(
                    id=q_id,
                    event_id=clean_id,
                    label=q_label.strip(),
                    question_type=q_type,
                    required=q_req,
                    options=q_opts,
                    order_idx=idx,
                ))

    # Assign creator as organizer
    if creator_account_id:
        role = EventRole(
            id=f"er_{uuid.uuid4().hex[:12]}",
            event_id=clean_id,
            account_id=creator_account_id,
            role="organizer",
            created_at=datetime.now(timezone.utc),
        )
        db.add(role)

    record_audit_log(
        db,
        event_id=clean_id,
        action="event_created",
        payload={"name": event.name, "submissions_close": sub_close.isoformat()},
        account_id=creator_account_id,
        commit=False,
    )
    db.commit()
    db.refresh(event)
    return event


def update_event(
    db: Session,
    event_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    registration_open: Optional[datetime] = None,
    registration_close: Optional[datetime] = None,
    submissions_open: Optional[datetime] = None,
    submissions_close: Optional[datetime] = None,
    min_team_size: Optional[int] = None,
    max_team_size: Optional[int] = None,
    prizes: Optional[list] = None,
    actor_account_id: Optional[str] = None,
) -> Event:
    """Update event configuration and dates with validation."""
    event = db.get(Event, event_id)
    if not event:
        raise ValueError(f"Event '{event_id}' not found.")

    changes = {}
    if name is not None and name.strip():
        changes["name"] = {"old": event.name, "new": name.strip()}
        event.name = name.strip()
    if description is not None:
        event.description = description.strip()

    eff_reg_open = ensure_utc(registration_open) if registration_open is not None else event.registration_open
    eff_reg_close = ensure_utc(registration_close) if registration_close is not None else event.registration_close
    if eff_reg_open and eff_reg_close and eff_reg_open > eff_reg_close:
        raise ValueError("Registration open date must be before or equal to registration close date.")

    eff_sub_open = ensure_utc(submissions_open) if submissions_open is not None else event.submissions_open
    eff_sub_close = ensure_utc(submissions_close) if submissions_close is not None else event.submissions_close
    if eff_sub_open and eff_sub_close and eff_sub_open > eff_sub_close:
        raise ValueError("Submissions open date must be before or equal to submissions close date.")

    if registration_open is not None:
        event.registration_open = eff_reg_open
    if registration_close is not None:
        event.registration_close = eff_reg_close
    if submissions_open is not None:
        event.submissions_open = eff_sub_open
    if submissions_close is not None:
        changes["submissions_close"] = {"old": event.submissions_close.isoformat(), "new": eff_sub_close.isoformat()}
        event.submissions_close = eff_sub_close

    eff_min = min_team_size if min_team_size is not None else event.min_team_size
    eff_max = max_team_size if max_team_size is not None else event.max_team_size
    if eff_min < 1:
        raise ValueError("Minimum team size must be at least 1.")
    if eff_max < eff_min:
        raise ValueError("Maximum team size cannot be smaller than minimum team size.")

    if min_team_size is not None:
        event.min_team_size = min_team_size
    if max_team_size is not None:
        event.max_team_size = max_team_size
    if prizes is not None:
        event.prizes = prizes

    record_audit_log(
        db,
        event_id=event_id,
        action="event_updated",
        payload=changes,
        account_id=actor_account_id,
        commit=False,
    )
    db.commit()
    db.refresh(event)
    return event


# --- Team Management Service ---


def get_account_team_for_event(db: Session, event_id: str, account_id: str) -> Optional[Team]:
    """Retrieve the team an account belongs to within an event."""
    stmt = (
        select(Team)
        .join(TeamMembership, Team.id == TeamMembership.team_id)
        .where(
            TeamMembership.event_id == event_id,
            TeamMembership.account_id == account_id,
        )
    )
    return db.scalars(stmt).first()


def create_team(
    db: Session,
    event_id: str,
    name: str,
    creator_account: Account,
    team_id: Optional[str] = None,
) -> Team:
    """Create a new team, assigning creator as captain and enforcing one team per event."""
    event = db.get(Event, event_id)
    if not event:
        raise ValueError(f"Event '{event_id}' not found.")

    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Team name cannot be empty.")

    # Check database-enforced single team per account per event
    existing_team = get_account_team_for_event(db, event_id, creator_account.id)
    if existing_team:
        raise ValueError(
            f"You are already a member of team '{existing_team.name}' in this event. "
            "A participant can only belong to one team per event."
        )

    tid = team_id or f"tm_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)

    team = Team(
        id=tid,
        event_id=event_id,
        name=clean_name,
        members=[{
            "account_id": creator_account.id,
            "name": creator_account.name,
            "email": creator_account.email,
            "role": "captain",
        }],
        created_by_account_id=creator_account.id,
        captain_account_id=creator_account.id,
    )
    db.add(team)

    # Add team membership
    membership = TeamMembership(
        id=f"tmb_{uuid.uuid4().hex[:12]}",
        team_id=tid,
        account_id=creator_account.id,
        event_id=event_id,
        role="captain",
        joined_at=now,
    )
    db.add(membership)

    # Ensure creator has participant role for event
    if not db.scalars(
        select(EventRole).where(
            EventRole.event_id == event_id,
            EventRole.account_id == creator_account.id,
            EventRole.role == "participant",
        )
    ).first():
        db.add(EventRole(
            id=f"er_{uuid.uuid4().hex[:12]}",
            event_id=event_id,
            account_id=creator_account.id,
            role="participant",
            created_at=now,
        ))

    db.commit()
    db.refresh(team)

    record_audit_log(
        db,
        event_id=event_id,
        action="team_created",
        payload={"team_id": team.id, "team_name": team.name},
        account_id=creator_account.id,
    )
    return team


def create_team_invitation(
    db: Session,
    team_id: str,
    email: str,
    requester: Account,
) -> TeamInvitation:
    """Create an invitation token to join a team."""
    team = db.get(Team, team_id)
    if not team:
        raise ValueError(f"Team '{team_id}' not found.")

    # Verify requester is captain or platform admin
    if team.captain_account_id != requester.id and not requester.is_platform_admin:
        raise ValueError("Only the team captain can invite new members.")

    clean_email = normalize_email(email)

    # Check team capacity
    event = db.get(Event, team.event_id)
    current_members_count = db.scalar(
        select(func.count(TeamMembership.id)).where(TeamMembership.team_id == team_id)
    ) or 0
    if current_members_count >= event.max_team_size:
        raise ValueError(f"Team is at maximum capacity ({event.max_team_size} members).")

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)

    invitation = TeamInvitation(
        id=f"inv_{uuid.uuid4().hex[:12]}",
        team_id=team_id,
        email=clean_email,
        token=token,
        status="pending",
        expires_at=expires_at,
        created_at=now,
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)
    return invitation


def accept_team_invitation(
    db: Session,
    token: str,
    account: Account,
) -> TeamMembership:
    """Accept team invitation with row locking to ensure capacity and single-team integrity."""
    invitation = db.scalars(
        select(TeamInvitation).where(TeamInvitation.token == token)
    ).first()

    if not invitation:
        raise ValueError("Invalid invitation token.")

    if invitation.status != "pending":
        raise ValueError(f"Invitation has already been {invitation.status}.")

    now = datetime.now(timezone.utc)
    expires_at = ensure_utc(invitation.expires_at)
    if expires_at and expires_at < now:
        invitation.status = "expired"
        db.commit()
        raise ValueError("Invitation has expired.")

    # Concurrency safe team capacity check via SELECT FOR UPDATE (PostgreSQL) or row lock
    team_stmt = select(Team).where(Team.id == invitation.team_id)
    if db.bind.dialect.name == "postgresql":
        team_stmt = team_stmt.with_for_update()
    team = db.scalars(team_stmt).first()

    if not team:
        raise ValueError("Team no longer exists.")

    event = db.get(Event, team.event_id)

    # Check if user already on another team in this event
    existing_team = get_account_team_for_event(db, team.event_id, account.id)
    if existing_team:
        raise ValueError(
            f"You are already a member of team '{existing_team.name}' in this event. "
            "Leave your current team before joining a new one."
        )

    # Count current members
    member_count = db.scalar(
        select(func.count(TeamMembership.id)).where(TeamMembership.team_id == team.id)
    ) or 0
    if member_count >= event.max_team_size:
        raise ValueError(f"Team has reached maximum capacity of {event.max_team_size} members.")

    membership = TeamMembership(
        id=f"tmb_{uuid.uuid4().hex[:12]}",
        team_id=team.id,
        account_id=account.id,
        event_id=team.event_id,
        role="member",
        joined_at=now,
    )
    db.add(membership)

    # Update team members JSON
    updated_members = list(team.members)
    updated_members.append({
        "account_id": account.id,
        "name": account.name,
        "email": account.email,
        "role": "member",
    })
    team.members = updated_members

    invitation.status = "accepted"

    # Ensure user has participant role
    if not db.scalars(
        select(EventRole).where(
            EventRole.event_id == team.event_id,
            EventRole.account_id == account.id,
            EventRole.role == "participant",
        )
    ).first():
        db.add(EventRole(
            id=f"er_{uuid.uuid4().hex[:12]}",
            event_id=team.event_id,
            account_id=account.id,
            role="participant",
            created_at=now,
        ))

    db.commit()
    db.refresh(membership)
    return membership


# --- Project Submission Lifecycle Service ---


def save_project_draft_or_submission(
    db: Session,
    event_id: str,
    team_id: str,
    account: Account,
    title: str,
    summary: str,
    repo_url: str = "",
    track_id: Optional[str] = None,
    tagline: str = "",
    description: str = "",
    demo_url: str = "",
    video_url: str = "",
    tech_tags: Optional[list] = None,
    thumbnail_url: Optional[str] = None,
    gallery_urls: Optional[list] = None,
    answers: Optional[dict] = None,
    is_final_submission: bool = False,
    project_id: Optional[str] = None,
    expected_version: Optional[int] = None,
    new_project_id: Optional[str] = None,
) -> Project:
    """Create or update a project draft or final submission with deadline and concurrency enforcement."""
    event = db.get(Event, event_id)
    if not event:
        raise ValueError(f"Event '{event_id}' not found.")

    team = db.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise ValueError("Invalid team for this event.")

    # Check team membership
    membership = db.scalars(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id,
            TeamMembership.account_id == account.id,
        )
    ).first()
    if not membership and not account.is_platform_admin:
        raise ValueError("You must be a member of this team to manage its project.")

    now = datetime.now(timezone.utc)
    s_open = ensure_utc(event.submissions_open)
    s_close = ensure_utc(event.submissions_close)

    # Server-side deadline check (inclusive cutoff: now >= s_close refuses submission)
    if s_open and now < s_open:
        raise ValueError(f"Submissions for {event.name} have not opened yet.")
    if s_close and now >= s_close:
        raise ValueError(f"Submissions for {event.name} closed on {s_close.isoformat()}.")

    # If track_id not specified, pick first track of the event
    if not track_id:
        first_track = db.scalars(select(Track).where(Track.event_id == event_id)).first()
        if not first_track:
            raise ValueError("No tracks configured for this event.")
        track_id = first_track.id
    else:
        track = db.get(Track, track_id)
        if not track or track.event_id != event_id:
            raise ValueError(f"Track '{track_id}' does not belong to event '{event_id}'.")

    # Check if team already has a project or find by project_id
    existing_project = None
    if project_id:
        existing_project = db.get(Project, project_id)
        if not existing_project:
            raise ValueError(f"Project '{project_id}' not found.")
        if existing_project.team_id != team_id:
            raise ValueError("Project does not belong to your team.")
        if existing_project.event_id != event_id:
            raise ValueError("Project does not belong to this event.")
    else:
        existing_project = db.scalars(
            select(Project).where(
                Project.event_id == event_id,
                Project.team_id == team_id,
            )
        ).first()

    clean_title = title.strip()
    clean_summary = summary.strip()
    if not clean_title:
        raise ValueError("Project title cannot be empty.")
    if not clean_summary:
        raise ValueError("Project summary cannot be empty.")

    # Validate required custom questions on final submission
    if is_final_submission:
        req_questions = db.scalars(
            select(EventQuestion).where(
                EventQuestion.event_id == event_id,
                EventQuestion.required == True,
            )
        ).all()
        for q in req_questions:
            ans = (answers or {}).get(q.id) or (answers or {}).get(q.label)
            if not ans or (isinstance(ans, str) and not ans.strip()):
                raise ValueError(f"Required question '{q.label}' must be answered before final submission.")

    target_status = "submitted" if is_final_submission else "draft"

    if existing_project:
        # Optimistic concurrency check
        if expected_version is not None and existing_project.version != expected_version:
            raise ConcurrencyConflictError(
                f"Conflict: Project has been modified by another teammate (current version {existing_project.version}, expected version {expected_version}). Please reload and retry."
            )

        # Update existing project
        existing_project.title = clean_title
        existing_project.summary = clean_summary
        existing_project.repo_url = repo_url.strip()
        existing_project.track_id = track_id
        existing_project.tagline = tagline.strip()
        existing_project.description = description.strip()
        existing_project.demo_url = demo_url.strip()
        existing_project.video_url = video_url.strip()
        existing_project.tech_tags = tech_tags or []
        existing_project.status = target_status
        if thumbnail_url:
            existing_project.thumbnail_url = thumbnail_url
        if gallery_urls is not None:
            existing_project.gallery_urls = gallery_urls
        if answers is not None:
            existing_project.answers = answers
        existing_project.version += 1
        existing_project.updated_at = now
        if is_final_submission:
            existing_project.submitted_at = now

        record_audit_log(
            db,
            event_id=event_id,
            action="project_submitted" if is_final_submission else "project_updated",
            payload={"project_id": existing_project.id, "title": existing_project.title, "status": target_status, "version": existing_project.version},
            account_id=account.id,
            commit=False,
        )
        db.commit()
        db.refresh(existing_project)
        return existing_project

    # Create new project
    pid = new_project_id or project_id or f"prj_{uuid.uuid4().hex[:10]}"
    project = Project(
        id=pid,
        event_id=event_id,
        team_id=team_id,
        track_id=track_id,
        title=clean_title,
        summary=clean_summary,
        repo_url=repo_url.strip(),
        submitted_at=now,
        tagline=tagline.strip(),
        description=description.strip(),
        demo_url=demo_url.strip(),
        video_url=video_url.strip(),
        tech_tags=tech_tags or [],
        status=target_status,
        thumbnail_url=thumbnail_url,
        gallery_urls=gallery_urls or [],
        answers=answers or {},
        version=1,
        updated_at=now,
    )
    db.add(project)

    record_audit_log(
        db,
        event_id=event_id,
        action="project_submitted" if is_final_submission else "project_created",
        payload={"project_id": project.id, "title": project.title, "status": target_status, "version": project.version},
        account_id=account.id,
        commit=False,
    )
    db.commit()
    db.refresh(project)
    return project


def revoke_team_invitation(
    db: Session,
    team_id: str,
    invitation_id: str,
    account: Optional[Account] = None,
    requester: Optional[Account] = None,
) -> TeamInvitation:
    """Revoke a pending team invitation by team captain or platform admin."""
    acting_account = account or requester
    if not acting_account:
        raise ValueError("Acting account is required to revoke an invitation.")

    team = db.get(Team, team_id)
    if not team:
        raise ValueError(f"Team '{team_id}' not found.")

    if team.captain_account_id != acting_account.id and not acting_account.is_platform_admin:
        raise ValueError("Only the team captain or an administrator can revoke invitations.")

    invitation = db.scalars(
        select(TeamInvitation).where(
            TeamInvitation.id == invitation_id,
            TeamInvitation.team_id == team_id,
        )
    ).first()
    if not invitation:
        raise ValueError(f"Invitation '{invitation_id}' not found for team '{team_id}'.")

    if invitation.status != "pending":
        raise ValueError(f"Cannot revoke invitation with status '{invitation.status}'. Only pending invitations can be revoked.")

    invitation.status = "revoked"
    record_audit_log(
        db,
        event_id=team.event_id,
        action="team_invitation_revoked",
        payload={"team_id": team_id, "invitation_id": invitation_id, "email": invitation.email},
        account_id=acting_account.id,
        commit=False,
    )
    db.commit()
    db.refresh(invitation)
    return invitation
