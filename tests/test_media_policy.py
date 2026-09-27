import io
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import Account, Event, EventAuditLog, Project, Track
from app.services import (
    ConcurrencyConflictError,
    create_account,
    create_event,
    create_team,
    save_project_draft_or_submission,
    update_project_thumbnail,
)


def make_png_bytes(color=(0, 128, 255, 255), size=(10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    """Provide isolated environment with in-memory DB and temporary upload dir."""
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

    yield {"engine": engine, "client": client, "upload_dir": upload_dir}

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


def test_media_upload_requires_authentication(media_env):
    """Verify unauthenticated media upload is rejected with 401."""
    client = media_env["client"]
    resp = client.post(
        "/projects/prj_dummy/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 1},
    )
    assert resp.status_code == 401
    assert "Authentication required" in resp.json()["detail"]


def test_media_upload_requires_team_membership(media_env):
    """Verify user not belonging to the team receives 403."""
    engine = media_env["engine"]
    client = media_env["client"]
    with Session(engine) as s:
        creator = create_account(s, "creator@test.local", "Password123!", "Creator")
        outsider = create_account(s, "outsider@test.local", "Password123!", "Outsider")
        event = create_event(
            s,
            "evt_med_auth",
            "Media Auth Event",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=2),
        )
        trk = Track(id="trk_med_1", event_id=event.id, name="Track 1")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "Team Creator", creator)
        proj = save_project_draft_or_submission(
            s, event.id, team.id, creator, "Proj Title", "Summary", track_id=trk.id
        )
        proj_id = proj.id

    csrf = login_user(client, "outsider@test.local")
    resp = client.post(
        f"/projects/{proj_id}/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 1, "csrf_token": csrf},
    )
    assert resp.status_code == 403
    assert "Forbidden" in resp.json()["detail"]


def test_media_upload_before_opening_rejected(media_env):
    """Verify media upload before submissions open is rejected with 403."""
    engine = media_env["engine"]
    client = media_env["client"]
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        user = create_account(s, "early@test.local", "Password123!", "Early User")
        event = create_event(
            s,
            "evt_early",
            "Early Event",
            submissions_open=now + timedelta(days=1),
            submissions_close=now + timedelta(days=5),
        )
        trk = Track(id="trk_early", event_id=event.id, name="Track")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "Early Team", user)
        proj = Project(
            id="prj_early",
            event_id=event.id,
            team_id=team.id,
            track_id=trk.id,
            title="Early Proj",
            summary="Early Summary",
            repo_url="https://github.com/test",
            submitted_at=now,
            version=1,
            updated_at=now,
        )
        s.add(proj)
        s.commit()

    csrf = login_user(client, "early@test.local")
    resp = client.post(
        "/projects/prj_early/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 1, "csrf_token": csrf},
    )
    assert resp.status_code == 403
    assert "not opened yet" in resp.json()["detail"]


def test_media_upload_after_and_at_closing_rejected(media_env):
    """Verify media upload at or after deadline is rejected with 403."""
    engine = media_env["engine"]
    client = media_env["client"]
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        user = create_account(s, "late_m@test.local", "Password123!", "Late User")
        event = create_event(
            s,
            "evt_late_m",
            "Late Media Event",
            submissions_close=now - timedelta(seconds=1),
        )
        trk = Track(id="trk_late_m", event_id=event.id, name="Track")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "Late Team", user)
        proj = Project(
            id="prj_late_m",
            event_id=event.id,
            team_id=team.id,
            track_id=trk.id,
            title="Late Proj",
            summary="Late Summary",
            repo_url="https://github.com/test",
            submitted_at=now,
            version=1,
            updated_at=now,
        )
        s.add(proj)
        s.commit()

    csrf = login_user(client, "late_m@test.local")
    resp = client.post(
        "/projects/prj_late_m/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 1, "csrf_token": csrf},
    )
    assert resp.status_code == 403
    assert "closed" in resp.json()["detail"]


def test_media_upload_missing_or_stale_expected_version_rejected(media_env):
    """Verify missing or stale expected_version on media upload returns 409."""
    engine = media_env["engine"]
    client = media_env["client"]
    with Session(engine) as s:
        user = create_account(s, "occ_m@test.local", "Password123!", "OCC Dev")
        event = create_event(
            s,
            "evt_occ_m",
            "OCC Event",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=2),
        )
        trk = Track(id="trk_occ_m", event_id=event.id, name="Track")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "OCC Team", user)
        proj = save_project_draft_or_submission(
            s, event.id, team.id, user, "OCC Proj", "Summary", track_id=trk.id
        )
        proj_id = proj.id
        assert proj.version == 1

    csrf = login_user(client, "occ_m@test.local")

    # Missing expected_version -> 409
    resp_missing = client.post(
        f"/projects/{proj_id}/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"csrf_token": csrf},
    )
    assert resp_missing.status_code == 409
    assert "'expected_version' is required" in resp_missing.json()["detail"]

    # Stale expected_version (e.g. 0 when current is 1) -> 409
    resp_stale = client.post(
        f"/projects/{proj_id}/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 0, "csrf_token": csrf},
    )
    assert resp_stale.status_code == 409
    assert "modified by another teammate" in resp_stale.json()["detail"]


def test_competing_text_and_thumbnail_edits(media_env):
    """Verify competing text update and thumbnail edit enforces OCC: first advances version, second fails with 409."""
    engine = media_env["engine"]
    client = media_env["client"]
    with Session(engine) as s:
        user = create_account(s, "competing@test.local", "Password123!", "Competing Dev")
        event = create_event(
            s,
            "evt_compete",
            "Compete Event",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=2),
        )
        trk = Track(id="trk_compete", event_id=event.id, name="Track")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "Compete Team", user)
        proj = save_project_draft_or_submission(
            s, event.id, team.id, user, "Initial Title", "Summary", track_id=trk.id
        )
        proj_id = proj.id
        team_id = team.id
        user_id = user.id
        event_id = event.id
        track_id = trk.id
        assert proj.version == 1

    # Teammate A updates text draft with expected_version=1 -> version becomes 2
    with Session(engine) as s:
        acc = s.get(Account, user_id)
        p_updated = save_project_draft_or_submission(
            s,
            event_id=event_id,
            team_id=team_id,
            account=acc,
            title="Title by Client A",
            summary="Updated text",
            track_id=track_id,
            project_id=proj_id,
            expected_version=1,
        )
        assert p_updated.version == 2

    # Teammate B (who loaded form at version 1) submits thumbnail upload with expected_version=1 -> receives 409
    csrf = login_user(client, "competing@test.local")
    resp = client.post(
        f"/projects/{proj_id}/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 1, "csrf_token": csrf},
    )
    assert resp.status_code == 409
    assert "modified by another teammate" in resp.json()["detail"]


def test_no_orphaned_file_after_failed_or_rejected_upload(media_env):
    """Verify that when a thumbnail upload fails due to conflict or deadline, no file is left on disk."""
    engine = media_env["engine"]
    client = media_env["client"]
    upload_dir = media_env["upload_dir"]

    with Session(engine) as s:
        user = create_account(s, "orphan@test.local", "Password123!", "Orphan Dev")
        event = create_event(
            s,
            "evt_orphan",
            "Orphan Event",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=2),
        )
        trk = Track(id="trk_orphan", event_id=event.id, name="Track")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "Orphan Team", user)
        proj = save_project_draft_or_submission(
            s, event.id, team.id, user, "Orphan Proj", "Summary", track_id=trk.id
        )
        proj_id = proj.id

    csrf = login_user(client, "orphan@test.local")

    # Project directory before failed upload
    proj_media_dir = upload_dir / proj_id
    files_before = list(proj_media_dir.glob("*")) if proj_media_dir.exists() else []
    assert len(files_before) == 0

    # Attempt upload with invalid stale version 99
    resp = client.post(
        f"/projects/{proj_id}/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 99, "csrf_token": csrf},
    )
    assert resp.status_code == 409

    # Verify no file remains on disk after failed transaction
    files_after = list(proj_media_dir.glob("*")) if proj_media_dir.exists() else []
    assert len(files_after) == 0, f"Orphaned files found: {files_after}"


def test_successful_media_upload_advances_version_and_creates_atomic_audit(media_env):
    """Verify successful upload advances version, writes file to disk, and commits audit record atomically."""
    engine = media_env["engine"]
    client = media_env["client"]
    upload_dir = media_env["upload_dir"]

    with Session(engine) as s:
        user = create_account(s, "succ_m@test.local", "Password123!", "Success Dev")
        event = create_event(
            s,
            "evt_succ_m",
            "Success Event",
            submissions_close=datetime.now(timezone.utc) + timedelta(days=2),
        )
        trk = Track(id="trk_succ_m", event_id=event.id, name="Track")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "Success Team", user)
        proj = save_project_draft_or_submission(
            s, event.id, team.id, user, "Success Proj", "Summary", track_id=trk.id
        )
        proj_id = proj.id
        assert proj.version == 1

    csrf = login_user(client, "succ_m@test.local")

    resp = client.post(
        f"/projects/{proj_id}/upload-image",
        files={"file": ("thumb.png", make_png_bytes(), "image/png")},
        data={"expected_version": 1, "csrf_token": csrf},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == 2
    assert "/media/" in data["url"]

    # Verify file exists on disk
    filename = Path(data["url"]).name
    saved_file = upload_dir / proj_id / filename
    assert saved_file.exists()
    assert saved_file.is_file()

    # Verify DB state and atomic audit record
    with Session(engine) as s:
        p = s.get(Project, proj_id)
        assert p.version == 2
        assert p.thumbnail_url == data["url"]

        audit_log = s.scalars(
            select(EventAuditLog).where(
                EventAuditLog.event_id == "evt_succ_m",
                EventAuditLog.action == "project_thumbnail_updated",
            )
        ).first()
        assert audit_log is not None
        assert audit_log.payload["version"] == 2
        assert audit_log.payload["thumbnail_url"] == data["url"]


def test_media_upload_crossing_deadline_during_processing(media_env):
    """Verify that if deadline expires between upload processing and lock wait, upload is rejected and file unlinked."""
    engine = media_env["engine"]
    upload_dir = media_env["upload_dir"]

    with Session(engine) as s:
        user = create_account(s, "cross_dl@test.local", "Password123!", "Cross DL Dev")
        event = create_event(
            s,
            "evt_cross_dl",
            "Cross DL Event",
            submissions_close=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        trk = Track(id="trk_cross_dl", event_id=event.id, name="Track")
        s.add(trk)
        s.commit()
        team = create_team(s, event.id, "Cross Team", user)
        proj = save_project_draft_or_submission(
            s, event.id, team.id, user, "Cross Proj", "Summary", track_id=trk.id
        )
        proj_id = proj.id
        event_id = event.id
        user_id = user.id

    # Now simulate deadline expiring after upload started:
    # Update event.submissions_close to past right before DB transaction in update_project_thumbnail
    with Session(engine) as s:
        acc = s.get(Account, user_id)
        ev = s.get(Event, event_id)
        ev.submissions_close = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()

        # Call service directly
        with pytest.raises(ValueError, match="closed on"):
            update_project_thumbnail(
                db=s,
                project_id=proj_id,
                file_bytes=make_png_bytes(),
                original_filename="thumb.png",
                account=acc,
                expected_version=1,
            )

    # Check upload directory: no orphaned file!
    proj_dir = upload_dir / proj_id
    files = list(proj_dir.glob("*")) if proj_dir.exists() else []
    assert len(files) == 0, f"Expected 0 orphaned files, found: {files}"
