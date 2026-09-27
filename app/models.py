from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    submissions_close: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Track(Base):
    __tablename__ = "tracks"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(200))


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(200))
    members: Mapped[list] = mapped_column(JSON)


class Judge(Base):
    __tablename__ = "judges"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    tracks: Mapped[list] = mapped_column(JSON)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id"))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    repo_url: Mapped[str] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Review(Base):
    __tablename__ = "reviews"
    judge_id: Mapped[str] = mapped_column(ForeignKey("judges.id"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    criteria: Mapped[dict] = mapped_column(JSON)
    comment: Mapped[str] = mapped_column(Text)
