import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from sqlalchemy import text
from sqlalchemy.orm import Session
from .config import get_settings
from .database import SessionLocal, build_engine
from .models import Event, Track, Team, Judge, Project, Review

logger = logging.getLogger("hackforge.seed")

# Deterministic advisory lock identifier for PostgreSQL ("HACK" in hex)
POSTGRES_ADVISORY_LOCK_ID = 0x4841434B
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures.json"
FIXTURES_PATH = FIXTURES


class FixtureValidationError(Exception):
    """Raised when fixtures data fails structure, schema, or referential integrity checks."""
    pass


def parse_timestamp(value: str) -> datetime:
    """Parse ISO-8601 UTC timestamp to timezone-aware datetime."""
    if not value or not isinstance(value, str):
        raise FixtureValidationError(f"Invalid timestamp format: {value}")
    # Normalize Z to +00:00 for cross-version compatibility
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_fixtures_data(data: dict[str, Any]) -> None:
    """Validate structure, completeness, uniqueness, and referential integrity of fixtures data."""
    required_sections = ["event", "tracks", "teams", "judges", "projects", "scores"]
    for section in required_sections:
        if section not in data:
            raise FixtureValidationError(f"Missing required section in fixtures: '{section}'")

    event = data["event"]
    for key in ["id", "name", "submissions_close"]:
        if key not in event or not event[key]:
            raise FixtureValidationError(f"Event missing required field: '{key}'")
    parse_timestamp(event["submissions_close"])

    # Tracks
    track_ids = set()
    for t in data["tracks"]:
        if "id" not in t or "name" not in t:
            raise FixtureValidationError(f"Track missing required fields: {t}")
        if t["id"] in track_ids:
            raise FixtureValidationError(f"Contradictory duplicate track ID in fixtures: {t['id']}")
        track_ids.add(t["id"])

    # Teams
    team_ids = set()
    for tm in data["teams"]:
        if "id" not in tm or "name" not in tm or "members" not in tm:
            raise FixtureValidationError(f"Team missing required fields: {tm}")
        if tm["id"] in team_ids:
            raise FixtureValidationError(f"Contradictory duplicate team ID in fixtures: {tm['id']}")
        team_ids.add(tm["id"])

    # Judges
    judge_ids = set()
    for j in data["judges"]:
        if "id" not in j or "name" not in j or "email" not in j or "tracks" not in j:
            raise FixtureValidationError(f"Judge missing required fields: {j}")
        if j["id"] in judge_ids:
            raise FixtureValidationError(f"Contradictory duplicate judge ID in fixtures: {j['id']}")
        judge_ids.add(j["id"])

    # Projects
    project_ids = set()
    for p in data["projects"]:
        for key in ["id", "team", "track", "title", "summary", "repo_url", "submitted_at"]:
            if key not in p:
                raise FixtureValidationError(f"Project missing required field '{key}': {p}")
        if p["id"] in project_ids:
            raise FixtureValidationError(f"Contradictory duplicate project ID in fixtures: {p['id']}")
        project_ids.add(p["id"])

        if p["team"] not in team_ids:
            raise FixtureValidationError(
                f"Referential integrity failure: project '{p['id']}' references unknown team '{p['team']}'"
            )
        if p["track"] not in track_ids:
            raise FixtureValidationError(
                f"Referential integrity failure: project '{p['id']}' references unknown track '{p['track']}'"
            )
        parse_timestamp(p["submitted_at"])

    # Scores / Reviews
    review_pairs = set()
    for s in data["scores"]:
        for key in ["judge", "project", "criteria"]:
            if key not in s:
                raise FixtureValidationError(f"Score missing required field '{key}': {s}")
        pair = (s["judge"], s["project"])
        if pair in review_pairs:
            raise FixtureValidationError(
                f"Contradictory duplicate review for judge '{s['judge']}' and project '{s['project']}'"
            )
        review_pairs.add(pair)

        if s["judge"] not in judge_ids:
            raise FixtureValidationError(
                f"Referential integrity failure: review references unknown judge '{s['judge']}'"
            )
        if s["project"] not in project_ids:
            raise FixtureValidationError(
                f"Referential integrity failure: review references unknown project '{s['project']}'"
            )


def seed(session: Session, path: str | Path | None = None, reset: bool = False) -> dict[str, int]:
    """Import official hackathon fixtures into the database.

    - Validates entire payload before committing.
    - Preserves existing records and local edits (insert-if-not-exists).
    - Preserves all project records, including deliberate duplicates (prj_07 and prj_41).
    - Preserves missing reviews and constant scores.
    - Safe for concurrent startup via PostgreSQL advisory locks.
    - Never resets data unless `reset=True` is explicitly passed.
    """
    settings = get_settings()
    fixture_path = Path(path or settings.fixture_path)
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {fixture_path}")

    # Acquire PostgreSQL transaction-level advisory lock if running on PostgreSQL
    try:
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            session.execute(text(f"SELECT pg_advisory_xact_lock({POSTGRES_ADVISORY_LOCK_ID})"))
            logger.info("Acquired PostgreSQL advisory lock for fixture seeding.")
    except Exception as exc:
        logger.debug(f"Advisory lock check skipped/inapplicable: {exc}")

    raw_text = fixture_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    # Validate before touching database
    validate_fixtures_data(data)

    if reset:
        logger.warning("Operator requested explicit database reset before seeding.")
        session.query(Review).delete()
        session.query(Project).delete()
        session.query(Judge).delete()
        session.query(Team).delete()
        session.query(Track).delete()
        session.query(Event).delete()
        session.flush()

    event_data = data["event"]
    event_id = event_data["id"]

    stats = {
        "events_inserted": 0,
        "tracks_inserted": 0,
        "teams_inserted": 0,
        "judges_inserted": 0,
        "projects_inserted": 0,
        "reviews_inserted": 0,
    }

    # Event
    if session.get(Event, event_id) is None:
        session.add(Event(
            id=event_id,
            name=event_data["name"],
            submissions_close=parse_timestamp(event_data["submissions_close"]),
        ))
        stats["events_inserted"] += 1
    session.flush()

    # Tracks
    for t in data["tracks"]:
        if session.get(Track, t["id"]) is None:
            session.add(Track(id=t["id"], event_id=event_id, name=t["name"]))
            stats["tracks_inserted"] += 1
    session.flush()

    # Teams
    for tm in data["teams"]:
        if session.get(Team, tm["id"]) is None:
            session.add(Team(id=tm["id"], event_id=event_id, name=tm["name"], members=tm["members"]))
            stats["teams_inserted"] += 1
    session.flush()

    # Judges
    for j in data["judges"]:
        if session.get(Judge, j["id"]) is None:
            session.add(Judge(
                id=j["id"],
                event_id=event_id,
                name=j["name"],
                email=j["email"],
                tracks=j["tracks"],
            ))
            stats["judges_inserted"] += 1
    session.flush()

    # Projects (Preserves deliberate duplicate prj_07 and prj_41 with distinct IDs)
    for p in data["projects"]:
        if session.get(Project, p["id"]) is None:
            session.add(Project(
                id=p["id"],
                event_id=event_id,
                team_id=p["team"],
                track_id=p["track"],
                title=p["title"],
                summary=p["summary"],
                repo_url=p["repo_url"],
                submitted_at=parse_timestamp(p["submitted_at"]),
            ))
            stats["projects_inserted"] += 1
    session.flush()

    # Reviews / Scores (Preserves missing-review and constant-score cases)
    for s in data["scores"]:
        pair = (s["judge"], s["project"])
        if session.get(Review, pair) is None:
            session.add(Review(
                judge_id=s["judge"],
                project_id=s["project"],
                criteria=s["criteria"],
                comment=s.get("comment", ""),
            ))
            stats["reviews_inserted"] += 1
    session.flush()

    logger.info(f"Seeding completed. Stats: {stats}")
    return stats


def main():
    parser = argparse.ArgumentParser(description="HackForge Fixture Seeder")
    parser.add_argument("--fixtures", default=None, help="Path to fixtures.json")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DESTRUCTIVE: Wipe existing data and reload fresh fixtures",
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        with session.begin():
            stats = seed(session, path=args.fixtures, reset=args.reset)
    print(f"HackForge: official fixtures loaded successfully. Stats: {stats}")


if __name__ == "__main__":
    main()
