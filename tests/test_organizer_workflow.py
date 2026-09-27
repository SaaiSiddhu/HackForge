import uuid
import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import Event, EventQuestion, EventRole, Project, Track
from app.services import create_account, create_event, create_team


@pytest.fixture
def org_env(tmp_path, monkeypatch):
    """Provide isolated environment with in-memory DB and test client."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", upload_dir)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield {"engine": engine, "client": client}

    app.dependency_overrides.clear()
    engine.dispose()


def login_user(client: TestClient, email: str, password: str = "Password123!") -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    login_resp = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert login_resp.status_code == 302, f"Login failed for {email}"
    return client.cookies.get("csrf_token")


def test_organizer_e2e_configuration_and_isolation_flow(org_env):
    """End-to-end integration test driving organizer settings through HTTP requests."""
    engine = org_env["engine"]
    client = org_env["client"]
    now = datetime.now(timezone.utc)

    # 1. Setup two events: Event A and Event B, each with its own organizer
    with Session(engine) as s:
        org_a = create_account(s, "org_a@test.local", "Password123!", "Organizer A")
        org_b = create_account(s, "org_b@test.local", "Password123!", "Organizer B")
        participant = create_account(s, "participant@test.local", "Password123!", "Participant A")

        event_a = create_event(
            s,
            event_id="evt_org_a",
            name="Event Alpha",
            registration_open=now - timedelta(days=10),
            registration_close=now + timedelta(days=10),
            submissions_open=now - timedelta(days=5),
            submissions_close=now + timedelta(days=5),
            creator_account_id=org_a.id,
        )
        event_b = create_event(
            s,
            event_id="evt_org_b",
            name="Event Beta",
            registration_open=now - timedelta(days=10),
            registration_close=now + timedelta(days=10),
            submissions_open=now - timedelta(days=5),
            submissions_close=now + timedelta(days=5),
            creator_account_id=org_b.id,
        )

    # --- Step 2: Validate Date Ordering on Event Edit ---
    csrf_a = login_user(client, "org_a@test.local")

    # Invalid registration dates: open > close -> must fail 400
    bad_reg_resp = client.post(
        "/events/evt_org_a/manage",
        data={
            "name": "Event Alpha",
            "registration_open": (now + timedelta(days=5)).isoformat(),
            "registration_close": (now + timedelta(days=1)).isoformat(),
            "submissions_close": (now + timedelta(days=10)).isoformat(),
            "csrf_token": csrf_a,
        },
    )
    assert bad_reg_resp.status_code == 400
    assert "Registration open date must be before or equal to registration close date" in bad_reg_resp.text

    # Invalid submissions dates: open > close -> must fail 400
    bad_sub_resp = client.post(
        "/events/evt_org_a/manage",
        data={
            "name": "Event Alpha",
            "submissions_open": (now + timedelta(days=15)).isoformat(),
            "submissions_close": (now + timedelta(days=10)).isoformat(),
            "csrf_token": csrf_a,
        },
    )
    assert bad_sub_resp.status_code == 400
    assert "Submissions open date must be before or equal to submissions close date" in bad_sub_resp.text

    # Valid update succeeds
    valid_update_resp = client.post(
        "/events/evt_org_a/manage",
        data={
            "name": "Event Alpha Updated",
            "description": "Updated Description",
            "min_team_size": 1,
            "max_team_size": 4,
            "registration_open": (now - timedelta(days=2)).isoformat(),
            "registration_close": (now + timedelta(days=2)).isoformat(),
            "submissions_open": (now - timedelta(days=1)).isoformat(),
            "submissions_close": (now + timedelta(days=7)).isoformat(),
            "csrf_token": csrf_a,
        },
        follow_redirects=False,
    )
    assert valid_update_resp.status_code == 302

    # --- Step 3: Verify Cross-Event Isolation (Organizer B cannot modify Event A) ---
    csrf_b = login_user(client, "org_b@test.local")

    # View Event A manage screen -> 403
    resp_view = client.get("/events/evt_org_a/manage")
    assert resp_view.status_code == 403

    # Post Event A manage settings -> 403
    resp_post = client.post(
        "/events/evt_org_a/manage",
        data={"name": "Hacked Event", "submissions_close": now.isoformat(), "csrf_token": csrf_b},
    )
    assert resp_post.status_code == 403

    # Add track to Event A -> 403
    resp_track = client.post(
        "/events/evt_org_a/tracks",
        data={"name": "Hacked Track", "csrf_token": csrf_b},
    )
    assert resp_track.status_code == 403

    # Add prize to Event A -> 403
    resp_prize = client.post(
        "/events/evt_org_a/prizes",
        data={"title": "Hacked Prize", "csrf_token": csrf_b},
    )
    assert resp_prize.status_code == 403

    # Add question to Event A -> 403
    resp_q = client.post(
        "/events/evt_org_a/questions",
        data={"label": "Hacked Question", "csrf_token": csrf_b},
    )
    assert resp_q.status_code == 403

    # --- Step 4: Organizer A creates Track, Prize, and Required Question ---
    csrf_a = login_user(client, "org_a@test.local")

    # Create new Track via JSON or form
    track_resp = client.post(
        "/events/evt_org_a/tracks",
        data={"name": "AI & Agent Systems", "csrf_token": csrf_a},
        follow_redirects=False,
    )
    assert track_resp.status_code == 302

    # Create new Prize
    prize_resp = client.post(
        "/events/evt_org_a/prizes",
        data={"title": "Grand Innovation Prize", "description": "Top overall submission", "amount": "$5,000", "csrf_token": csrf_a},
        follow_redirects=False,
    )
    assert prize_resp.status_code == 302

    # Create Required Custom Question
    q_resp = client.post(
        "/events/evt_org_a/questions",
        data={"label": "System Architecture Overview", "question_type": "text", "required": "true", "csrf_token": csrf_a},
        follow_redirects=False,
    )
    assert q_resp.status_code == 302

    # Verify entities in DB
    with Session(engine) as s:
        new_track = s.scalars(select(Track).where(Track.event_id == "evt_org_a", Track.name == "AI & Agent Systems")).first()
        assert new_track is not None
        new_track_id = new_track.id

        ev_a = s.get(Event, "evt_org_a")
        assert len(ev_a.prizes) >= 1
        assert ev_a.prizes[-1]["title"] == "Grand Innovation Prize"

        new_q = s.scalars(select(EventQuestion).where(EventQuestion.event_id == "evt_org_a")).first()
        assert new_q is not None
        assert new_q.required is True
        new_q_id = new_q.id

    # --- Step 5: Participant views submission form (renders track and required question) ---
    csrf_p = login_user(client, "participant@test.local")

    # Form team in Event Alpha
    team_resp = client.post(
        "/events/evt_org_a/teams/new",
        data={"name": "Alpha Builders", "csrf_token": csrf_p},
        follow_redirects=False,
    )
    assert team_resp.status_code == 302

    # Fetch submit page
    submit_page_resp = client.get("/events/evt_org_a/submit")
    assert submit_page_resp.status_code == 200
    assert ("AI &amp; Agent Systems" in submit_page_resp.text) or ("AI & Agent Systems" in submit_page_resp.text)
    assert "System Architecture Overview *" in submit_page_resp.text

    # --- Step 6: Submit without answering required question -> rejected (400) ---
    missing_ans_resp = client.post(
        "/events/evt_org_a/submit",
        data={
            "title": "Alpha Agent Project",
            "summary": "Building an autonomous system",
            "repo_url": "https://github.com/example/alpha-agent",
            "track_id": new_track_id,
            "action": "submit",
            "csrf_token": csrf_p,
            f"question_{new_q_id}": "",  # Empty answer to required question
        },
    )
    assert missing_ans_resp.status_code == 400
    assert ("Required question 'System Architecture Overview' must be answered" in missing_ans_resp.text) or ("Required question &#39;System Architecture Overview&#39; must be answered" in missing_ans_resp.text)

    # --- Step 7: Submit WITH answering required question -> succeeds (302) ---
    valid_submit_resp = client.post(
        "/events/evt_org_a/submit",
        data={
            "title": "Alpha Agent Project",
            "summary": "Building an autonomous system",
            "repo_url": "https://github.com/example/alpha-agent",
            "track_id": new_track_id,
            "action": "submit",
            "csrf_token": csrf_p,
            f"question_{new_q_id}": "Decentralized P2P agents with FastAPI and Postgres",
        },
        follow_redirects=False,
    )
    assert valid_submit_resp.status_code == 302

    # Verify project exists and references new_track_id
    with Session(engine) as s:
        proj = s.scalars(select(Project).where(Project.event_id == "evt_org_a")).first()
        assert proj is not None
        assert proj.track_id == new_track_id
        assert proj.answers[new_q_id] == "Decentralized P2P agents with FastAPI and Postgres"
        assert proj.status == "submitted"

    # --- Step 8: Deleting the referenced track is strictly rejected while project exists ---
    csrf_a = login_user(client, "org_a@test.local")
    del_track_resp = client.post(
        f"/events/evt_org_a/tracks/{new_track_id}/delete",
        data={"csrf_token": csrf_a},
    )
    assert del_track_resp.status_code == 400
    assert "referenced by 1 existing project submission" in del_track_resp.text

    # Track still exists
    with Session(engine) as s:
        track_still_exists = s.get(Track, new_track_id)
        assert track_still_exists is not None

    # --- Step 9: Delete custom question preserves historical answers in project ---
    del_q_resp = client.post(
        f"/events/evt_org_a/questions/{new_q_id}/delete",
        data={"csrf_token": csrf_a},
        follow_redirects=False,
    )
    assert del_q_resp.status_code == 302

    # Verify question row was removed but project answer dict retains the data
    with Session(engine) as s:
        q_row = s.get(EventQuestion, new_q_id)
        assert q_row is None

        proj_after = s.scalars(select(Project).where(Project.event_id == "evt_org_a")).first()
        assert proj_after.answers[new_q_id] == "Decentralized P2P agents with FastAPI and Postgres"
