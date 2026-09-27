import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from .config import get_settings
from .database import SessionLocal
from .models import (
    Account,
    Event,
    EventRole,
    Judge,
    Project,
    Review,
    SessionModel,
    Team,
    TeamMembership,
    Track,
)

logger = logging.getLogger("hackforge.seed")

# Deterministic advisory lock identifier for PostgreSQL ("HACK" in hex)
POSTGRES_ADVISORY_LOCK_ID = 0x4841434B
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures.json"
FIXTURES_PATH = FIXTURES


class FixtureValidationError(Exception):
    """Raised when fixtures data fails structure, schema, or referential integrity checks."""
    pass


def parse_timestamp(value: str) -> datetime:
    """Parse ISO-8601 UTC timestamp to timezone-aware datetime normalized to UTC."""
    if not value or not isinstance(value, str):
        raise FixtureValidationError(f"Invalid timestamp format: {value}")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        # Validate that judge track interests reference existing tracks
        for tr in j["tracks"]:
            if tr not in track_ids:
                raise FixtureValidationError(
                    f"Referential integrity failure: judge '{j['id']}' references unknown track '{tr}'"
                )

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


def seed(
    session: Session,
    path: str | Path | None = None,
    reset: bool = False,
    seed_demo: bool = False,
) -> dict[str, int]:
    """Import official hackathon fixtures into the database.

    - Acquires PostgreSQL advisory transaction lock and raises on failure.
    - Validates entire payload before committing.
    - Preserves existing records and local edits (insert-if-not-exists).
    - Preserves all project records, including deliberate duplicates (prj_07 and prj_41).
    - Preserves missing reviews and constant scores.
    - Never resets data unless `reset=True` is explicitly passed and environment permits.
    """
    settings = get_settings()
    fixture_path = Path(path or settings.fixture_path)
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {fixture_path}")

    # Acquire PostgreSQL transaction-level advisory lock if running on PostgreSQL
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        lock_acquired = session.execute(
            text(f"SELECT pg_try_advisory_xact_lock({POSTGRES_ADVISORY_LOCK_ID})")
        ).scalar()
        if not lock_acquired:
            raise RuntimeError(
                f"Could not acquire PostgreSQL advisory transaction lock ({POSTGRES_ADVISORY_LOCK_ID}); "
                "another seed, migration, or maintenance process is actively holding it."
            )
        logger.info("Acquired PostgreSQL advisory transaction lock for fixture seeding.")

    raw_text = fixture_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    # Validate before touching database
    validate_fixtures_data(data)

    if reset:
        if settings.environment == "production" or (
            settings.environment != "testing"
            and bind.dialect.name != "sqlite"
            and os.environ.get("ALLOW_DESTRUCTIVE_RESET", "").lower() not in ("true", "1", "yes")
        ):
            raise PermissionError(
                "Destructive reset is blocked in this environment. "
                "Set ALLOW_DESTRUCTIVE_RESET=true in non-production environments to allow reset."
            )
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

    # Seed Phase 2 demo accounts, sessions, and open demonstration event if requested
    if seed_demo:
        demo_stats = seed_demo_accounts_and_event(session)
        stats.update(demo_stats)

    logger.info(f"Seeding completed. Stats: {stats}")
    return stats


def seed_demo_accounts_and_event(session: Session) -> dict[str, int]:
    """Seed test accounts, session tokens for DOGFOOD test runner, and the open demo event."""
    from .auth import hash_password, hash_session_token

    settings = get_settings()
    env = (getattr(settings, "environment", "") or os.environ.get("ENVIRONMENT", "")).lower()
    app_env = os.environ.get("APP_ENV", "").lower()
    if env == "production" or app_env == "production":
        raise ValueError("Demo accounts and credentials are strictly prohibited in production environment.")

    demo_enabled = (
        getattr(settings, "enable_demo_provisioning", False)
        or os.environ.get("ENABLE_DEMO_PROVISIONING", "").lower() in ("true", "1", "yes")
    )
    if not demo_enabled:
        logger.info("Demo provisioning disabled (ENABLE_DEMO_PROVISIONING is false). Skipping demo accounts.")
        return {"accounts_inserted": 0, "sessions_inserted": 0}

    now = datetime.now(timezone.utc)
    future_expire = now + timedelta(days=365)
    default_pw_hash = hash_password("Password123!")

    stats = {
        "accounts_inserted": 0,
        "sessions_inserted": 0,
    }

    # 1. Test accounts matching .dogfood.toml
    demo_accounts = [
        {"id": "acc_org_demo", "email": "organizer@hackforge.local", "name": "Demo Organizer", "is_admin": True, "token": "org_demo"},
        {"id": "acc_prt_demo", "email": "participant@hackforge.local", "name": "Demo Participant", "is_admin": False, "token": "prt_demo"},
        {"id": "acc_jdg_a_demo", "email": "judge_a@hackforge.local", "name": "Judge Alpha", "is_admin": False, "token": "jdg_a_demo"},
        {"id": "acc_jdg_b_demo", "email": "judge_b@hackforge.local", "name": "Judge Beta", "is_admin": False, "token": "jdg_b_demo"},
    ]

    for da in demo_accounts:
        acc = session.get(Account, da["id"])
        if not acc:
            acc = Account(
                id=da["id"],
                email=da["email"],
                password_hash=default_pw_hash,
                name=da["name"],
                is_platform_admin=da["is_admin"],
                created_at=now,
            )
            session.add(acc)
            session.flush()
            stats["accounts_inserted"] += 1

        # Session token
        t_hash = hash_session_token(da["token"])
        sess = session.scalars(select(SessionModel).where(SessionModel.token_hash == t_hash)).first()
        if not sess:
            session.add(SessionModel(
                id=f"ses_{da['token']}",
                token_hash=t_hash,
                account_id=acc.id,
                expires_at=future_expire,
                created_at=now,
            ))
            stats["sessions_inserted"] += 1
        else:
            sess.expires_at = future_expire

    # 2. Open Demo Event (Open for live testing and submissions)
    open_evt_id = "evt_open_demo"
    if session.get(Event, open_evt_id) is None:
        session.add(Event(
            id=open_evt_id,
            name="HackForge Spring Demo Hackathon 2026",
            description="Open demonstration hackathon for testing live registrations, team invitations, and submissions.",
            registration_open=datetime(2026, 1, 1, tzinfo=timezone.utc),
            registration_close=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            submissions_open=datetime(2026, 1, 1, tzinfo=timezone.utc),
            submissions_close=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            min_team_size=1,
            max_team_size=5,
            prizes=[
                {"place": "1st", "title": "Grand Prize", "reward": "$5,000"},
                {"place": "2nd", "title": "Runner Up", "reward": "$2,500"},
            ],
            created_by_account_id="acc_org_demo",
        ))
        session.flush()

        # Add tracks for open event
        for trk_id, trk_name in [
            ("trk_demo_01", "Web & Cloud"),
            ("trk_demo_02", "AI & Machine Learning"),
            ("trk_demo_03", "Developer Tools"),
        ]:
            if session.get(Track, trk_id) is None:
                session.add(Track(id=trk_id, event_id=open_evt_id, name=trk_name))
        session.flush()

    # 3. Roles
    roles = [
        ("evt_01", "acc_org_demo", "organizer"),
        ("evt_01", "acc_prt_demo", "participant"),
        ("evt_01", "acc_jdg_a_demo", "judge"),
        ("evt_01", "acc_jdg_b_demo", "judge"),
        ("evt_open_demo", "acc_org_demo", "organizer"),
        ("evt_open_demo", "acc_prt_demo", "participant"),
    ]
    for eid, aid, rname in roles:
        existing_role = session.scalars(
            select(EventRole).where(
                EventRole.event_id == eid,
                EventRole.account_id == aid,
                EventRole.role == rname,
            )
        ).first()
        if not existing_role:
            session.add(EventRole(
                id=f"er_{eid}_{aid}_{rname}",
                event_id=eid,
                account_id=aid,
                role=rname,
                created_at=now,
            ))

    # 4. Team membership for demo participant
    # On evt_01: assign to tm_01 so participant belongs to an existing team in evt_01
    if not session.scalars(
        select(TeamMembership).where(
            TeamMembership.event_id == "evt_01",
            TeamMembership.account_id == "acc_prt_demo",
        )
    ).first():
        session.add(TeamMembership(
            id="tmb_evt01_prt_demo",
            team_id="tm_01",
            account_id="acc_prt_demo",
            event_id="evt_01",
            role="member",
            joined_at=now,
        ))

    # On evt_open_demo: create tm_open_demo if not exists
    if session.get(Team, "tm_open_demo") is None:
        session.add(Team(
            id="tm_open_demo",
            event_id=open_evt_id,
            name="Demo Innovators",
            members=[{"account_id": "acc_prt_demo", "name": "Demo Participant", "role": "captain"}],
            created_by_account_id="acc_prt_demo",
            captain_account_id="acc_prt_demo",
        ))
        session.flush()

    if not session.scalars(
        select(TeamMembership).where(
            TeamMembership.event_id == open_evt_id,
            TeamMembership.account_id == "acc_prt_demo",
        )
    ).first():
        session.add(TeamMembership(
            id="tmb_open_prt_demo",
            team_id="tm_open_demo",
            account_id="acc_prt_demo",
            event_id=open_evt_id,
            role="captain",
            joined_at=now,
        ))

    session.flush()
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
            stats = seed(session, path=args.fixtures, reset=args.reset, seed_demo=True)
    print(f"HackForge: official fixtures loaded successfully. Stats: {stats}")


if __name__ == "__main__":
    main()
