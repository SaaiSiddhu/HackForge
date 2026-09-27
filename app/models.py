from datetime import datetime
from urllib.parse import urlparse
from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    submissions_close: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    tracks: Mapped[list["Track"]] = relationship("Track", back_populates="event", cascade="all, delete-orphan")
    teams: Mapped[list["Team"]] = relationship("Team", back_populates="event", cascade="all, delete-orphan")
    judges: Mapped[list["Judge"]] = relationship("Judge", back_populates="event", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="event", cascade="all, delete-orphan")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="tracks")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="track")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    members: Mapped[list] = mapped_column(JSON, nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="teams")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="team")


class Judge(Base):
    __tablename__ = "judges"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    tracks: Mapped[list] = mapped_column(JSON, nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="judges")
    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="judge")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    event: Mapped["Event"] = relationship("Event", back_populates="projects")
    team: Mapped["Team"] = relationship("Team", back_populates="projects")
    track: Mapped["Track"] = relationship("Track", back_populates="projects")
    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="project")

    __table_args__ = (
        Index("ix_projects_title", "title"),
        Index("ix_projects_track_id", "track_id"),
        Index("ix_projects_team_id", "team_id"),
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


class Review(Base):
    __tablename__ = "reviews"

    judge_id: Mapped[str] = mapped_column(ForeignKey("judges.id"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    criteria: Mapped[dict] = mapped_column(JSON, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)

    judge: Mapped["Judge"] = relationship("Judge", back_populates="reviews")
    project: Mapped["Project"] = relationship("Project", back_populates="reviews")
