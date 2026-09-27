from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    sessions: Mapped[list["SessionModel"]] = relationship("SessionModel", back_populates="account", cascade="all, delete-orphan")
    roles: Mapped[list["EventRole"]] = relationship("EventRole", back_populates="account", cascade="all, delete-orphan")
    team_memberships: Mapped[list["TeamMembership"]] = relationship("TeamMembership", back_populates="account", cascade="all, delete-orphan")


class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    account: Mapped["Account"] = relationship("Account", back_populates="sessions")


class EventRole(Base):
    __tablename__ = "event_roles"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)  # "organizer", "judge", "participant"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    account: Mapped["Account"] = relationship("Account", back_populates="roles")
    event: Mapped["Event"] = relationship("Event", back_populates="roles")

    __table_args__ = (
        UniqueConstraint("event_id", "account_id", "role", name="uq_event_account_role"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="")
    registration_open: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    registration_close: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    submissions_open: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    submissions_close: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    min_team_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_team_size: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    target_reviews_per_project: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    prizes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=list)
    created_by_account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)

    tracks: Mapped[list["Track"]] = relationship("Track", back_populates="event", cascade="all, delete-orphan")
    teams: Mapped[list["Team"]] = relationship("Team", back_populates="event", cascade="all, delete-orphan")
    judges: Mapped[list["Judge"]] = relationship("Judge", back_populates="event", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="event", cascade="all, delete-orphan")
    roles: Mapped[list["EventRole"]] = relationship("EventRole", back_populates="event", cascade="all, delete-orphan")
    questions: Mapped[list["EventQuestion"]] = relationship("EventQuestion", back_populates="event", cascade="all, delete-orphan")
    audit_logs: Mapped[list["EventAuditLog"]] = relationship("EventAuditLog", back_populates="event", cascade="all, delete-orphan")
    rubrics: Mapped[list["Rubric"]] = relationship("Rubric", back_populates="event", cascade="all, delete-orphan")
    judge_invitations: Mapped[list["JudgeInvitation"]] = relationship("JudgeInvitation", back_populates="event", cascade="all, delete-orphan")
    judge_assignments: Mapped[list["JudgeAssignment"]] = relationship("JudgeAssignment", back_populates="event", cascade="all, delete-orphan")
    results_snapshots: Mapped[list["EventResultsSnapshot"]] = relationship("EventResultsSnapshot", back_populates="event", cascade="all, delete-orphan")

    def is_submissions_open(self, now: Optional[datetime] = None) -> bool:
        """Check if event is currently open for submissions."""
        cur = now or datetime.now(timezone.utc)
        if cur.tzinfo is None:
            cur = cur.replace(tzinfo=timezone.utc)
        if self.submissions_open:
            s_open = self.submissions_open
            if s_open.tzinfo is None:
                s_open = s_open.replace(tzinfo=timezone.utc)
            if cur < s_open:
                return False
        s_close = self.submissions_close
        if s_close.tzinfo is None:
            s_close = s_close.replace(tzinfo=timezone.utc)
        if cur >= s_close:
            return False
        return True


class EventQuestion(Base):
    __tablename__ = "event_questions"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    question_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "text", "select", "checkbox"
    options: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    order_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="questions")


class EventAuditLog(Base):
    __tablename__ = "event_audit_logs"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="audit_logs")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="tracks")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="track")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    members: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by_account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    captain_account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)

    event: Mapped["Event"] = relationship("Event", back_populates="teams")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="team")
    memberships: Mapped[list["TeamMembership"]] = relationship("TeamMembership", back_populates="team", cascade="all, delete-orphan")
    invitations: Mapped[list["TeamInvitation"]] = relationship("TeamInvitation", back_populates="team", cascade="all, delete-orphan")


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="member")  # "captain", "member"
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    team: Mapped["Team"] = relationship("Team", back_populates="memberships")
    account: Mapped["Account"] = relationship("Account", back_populates="team_memberships")

    __table_args__ = (
        UniqueConstraint("event_id", "account_id", name="uq_team_memberships_event_account"),
    )


class TeamInvitation(Base):
    __tablename__ = "team_invitations"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)  # "pending", "accepted", "declined", "expired"
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    team: Mapped["Team"] = relationship("Team", back_populates="invitations")


class Judge(Base):
    __tablename__ = "judges"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    tracks: Mapped[list] = mapped_column(JSON, nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="judges")
    account: Mapped[Optional["Account"]] = relationship("Account")
    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="judge")
    track_permissions: Mapped[list["JudgeTrackPermission"]] = relationship("JudgeTrackPermission", back_populates="judge", cascade="all, delete-orphan")
    assignments: Mapped[list["JudgeAssignment"]] = relationship("JudgeAssignment", back_populates="judge", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    tagline: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="")
    demo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="")
    video_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="")
    tech_tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=list)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="submitted")  # "draft", "submitted"
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gallery_urls: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=list)
    answers: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=True)

    event: Mapped["Event"] = relationship("Event", back_populates="projects")
    team: Mapped["Team"] = relationship("Team", back_populates="projects")
    track: Mapped["Track"] = relationship("Track", back_populates="projects")
    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="project")

    __table_args__ = (
        Index("ix_projects_title", "title"),
        Index("ix_projects_track_id", "track_id"),
        Index("ix_projects_team_id", "team_id"),
        Index("ix_projects_status", "status"),
    )

    @property
    def safe_repo_url(self) -> str | None:
        """Return repository URL only if using safe http/https scheme with a host."""
        if not self.repo_url:
            return None
        url = self.repo_url.strip()
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
                return url
        except Exception:
            return None
        return None

    @property
    def safe_demo_url(self) -> str | None:
        """Return demo URL only if using safe http/https scheme."""
        if not self.demo_url:
            return None
        url = self.demo_url.strip()
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
                return url
        except Exception:
            return None
        return None


class JudgeTrackPermission(Base):
    __tablename__ = "judge_tracks"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    judge_id: Mapped[str] = mapped_column(ForeignKey("judges.id", ondelete="CASCADE"), index=True, nullable=False)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), index=True, nullable=False)

    judge: Mapped["Judge"] = relationship("Judge", back_populates="track_permissions")
    track: Mapped["Track"] = relationship("Track")

    __table_args__ = (
        UniqueConstraint("judge_id", "track_id", name="uq_judge_tracks_judge_track"),
    )


class JudgeInvitation(Base):
    __tablename__ = "judge_invitations"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tracks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)  # "pending", "accepted", "revoked", "expired"
    invited_by_account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    accepted_by_account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="judge_invitations")
    invited_by: Mapped[Optional["Account"]] = relationship("Account", foreign_keys=[invited_by_account_id])
    accepted_by: Mapped[Optional["Account"]] = relationship("Account", foreign_keys=[accepted_by_account_id])


class JudgeAssignment(Base):
    __tablename__ = "judge_assignments"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    judge_id: Mapped[str] = mapped_column(ForeignKey("judges.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="judge_assignments")
    judge: Mapped["Judge"] = relationship("Judge", back_populates="assignments")
    project: Mapped["Project"] = relationship("Project")

    __table_args__ = (
        UniqueConstraint("judge_id", "project_id", name="uq_judge_assignments_judge_project"),
    )


class Rubric(Base):
    __tablename__ = "rubrics"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="Default Rubric", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="rubrics")
    criteria: Mapped[list["RubricCriterion"]] = relationship("RubricCriterion", back_populates="rubric", cascade="all, delete-orphan", order_by="RubricCriterion.order_idx")

    __table_args__ = (
        UniqueConstraint("event_id", "version", name="uq_rubrics_event_version"),
    )


class RubricCriterion(Base):
    __tablename__ = "rubric_criteria"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    rubric_id: Mapped[str] = mapped_column(ForeignKey("rubrics.id", ondelete="CASCADE"), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    min_score: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    max_score: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    order_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    rubric: Mapped["Rubric"] = relationship("Rubric", back_populates="criteria")

    __table_args__ = (
        UniqueConstraint("rubric_id", "key", name="uq_rubric_criteria_rubric_key"),
    )


class Review(Base):
    __tablename__ = "reviews"

    judge_id: Mapped[str] = mapped_column(ForeignKey("judges.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[Optional[str]] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=True)
    rubric_id: Mapped[Optional[str]] = mapped_column(ForeignKey("rubrics.id", ondelete="SET NULL"), nullable=True)
    criteria: Mapped[dict] = mapped_column(JSON, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="finalized", nullable=False)  # "draft", "finalized"
    raw_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    judge: Mapped["Judge"] = relationship("Judge", back_populates="reviews")
    project: Mapped["Project"] = relationship("Project", back_populates="reviews")
    event: Mapped[Optional["Event"]] = relationship("Event")
    rubric: Mapped[Optional["Rubric"]] = relationship("Rubric")
    criterion_scores: Mapped[list["ReviewCriterionScore"]] = relationship("ReviewCriterionScore", back_populates="review", cascade="all, delete-orphan")


class ReviewCriterionScore(Base):
    __tablename__ = "review_criterion_scores"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    judge_id: Mapped[str] = mapped_column(String(100), nullable=False)
    project_id: Mapped[str] = mapped_column(String(100), nullable=False)
    criterion_id: Mapped[Optional[str]] = mapped_column(ForeignKey("rubric_criteria.id", ondelete="SET NULL"), nullable=True)
    criterion_key: Mapped[str] = mapped_column(String(100), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)

    review: Mapped["Review"] = relationship("Review", back_populates="criterion_scores")
    criterion: Mapped[Optional["RubricCriterion"]] = relationship("RubricCriterion")

    __table_args__ = (
        ForeignKeyConstraint(["judge_id", "project_id"], ["reviews.judge_id", "reviews.project_id"], ondelete="CASCADE"),
        UniqueConstraint("judge_id", "project_id", "criterion_key", name="uq_review_criterion_scores_key"),
        Index("ix_review_criterion_scores_judge_proj", "judge_id", "project_id"),
    )


class EventResultsSnapshot(Base):
    __tablename__ = "event_results_snapshots"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False)
    created_by_account_id: Mapped[Optional[str]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    method: Mapped[str] = mapped_column(String(100), default="zscore_v1", nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    input_review_hashes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    results_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="results_snapshots")
    created_by: Mapped[Optional["Account"]] = relationship("Account")

    __table_args__ = (
        Index("ix_event_results_snapshots_created_at", "created_at"),
    )

