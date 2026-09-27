import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.database import Base
from app.models import Event, Track
from app.services import (
    create_account,
    create_event,
    create_team,
    save_project_draft_or_submission,
    validate_and_save_image,
)


@pytest.fixture
def sub_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_submission_draft_and_final_lifecycle(sub_db):
    """Verify draft vs submitted status transitions and version incrementing."""
    user = create_account(sub_db, email="dev@test.local", password="Password123!", name="Dev Alice")
    event = create_event(
        sub_db,
        event_id="evt_sub_test",
        name="Submission Test Event",
        submissions_close=datetime.now(timezone.utc) + timedelta(days=30),
    )
    track = Track(id="trk_sub_01", event_id=event.id, name="General Track")
    sub_db.add(track)
    sub_db.commit()

    team = create_team(sub_db, event_id="evt_sub_test", name="Builders", creator_account=user)

    # 1. Save initial draft
    proj = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Initial Draft Title",
        summary="A draft summary",
        repo_url="https://github.com/alice/project",
        track_id=track.id,
        is_final_submission=False,
    )
    assert proj.status == "draft"
    assert proj.version == 1

    # 2. Update draft
    proj_updated = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Refined Draft Title",
        summary="Updated draft summary",
        repo_url="https://github.com/alice/project-v2",
        track_id=track.id,
        is_final_submission=False,
        project_id=proj.id,
        expected_version=1,
    )
    assert proj_updated.status == "draft"
    assert proj_updated.version == 2
    assert proj_updated.title == "Refined Draft Title"

    # 3. Final submission
    proj_final = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Final Awesome Title",
        summary="Final project summary",
        repo_url="https://github.com/alice/project-final",
        track_id=track.id,
        is_final_submission=True,
        project_id=proj_updated.id,
        expected_version=2,
    )
    assert proj_final.status == "submitted"
    assert proj_final.version == 3


def test_submission_strictly_refuses_after_deadline(sub_db):
    """Verify submissions and edits are strictly rejected after event deadline."""
    user = create_account(sub_db, email="late@test.local", password="Password123!", name="Late Dev")
    # Event closed yesterday
    event = create_event(
        sub_db,
        event_id="evt_closed_test",
        name="Past Event",
        submissions_close=datetime.now(timezone.utc) - timedelta(days=1),
    )
    track = Track(id="trk_past", event_id=event.id, name="Past Track")
    sub_db.add(track)
    sub_db.commit()

    team = create_team(sub_db, event_id="evt_closed_test", name="Late Team", creator_account=user)

    with pytest.raises(ValueError, match="closed on"):
        save_project_draft_or_submission(
            db=sub_db,
            event_id=event.id,
            team_id=team.id,
            account=user,
            title="Late Project",
            summary="Attempting late submission",
            repo_url="https://github.com/example/late",
            track_id=track.id,
            is_final_submission=True,
        )


def test_image_upload_magic_bytes_and_size_validation(tmp_path, monkeypatch):
    """Verify Pillow image validation for JPEG, PNG, GIF, WebP, dimension limits, and size bounds."""
    import io
    from PIL import Image
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")

    # Valid PNG
    png_buf = io.BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(png_buf, format="PNG")
    png_url = validate_and_save_image(png_buf.getvalue(), "test.png", project_id="prj_test")
    assert png_url.startswith("/projects/prj_test/media/")
    assert png_url.endswith(".png")

    # Valid JPEG
    jpg_buf = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 255, 0)).save(jpg_buf, format="JPEG")
    jpeg_url = validate_and_save_image(jpg_buf.getvalue(), "test.jpg", project_id="prj_test")
    assert jpeg_url.startswith("/projects/prj_test/media/")
    assert jpeg_url.endswith(".jpg")

    # Valid GIF
    gif_buf = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 0, 255)).save(gif_buf, format="GIF")
    gif_url = validate_and_save_image(gif_buf.getvalue(), "test.gif", project_id="prj_test")
    assert gif_url.startswith("/projects/prj_test/media/")
    assert gif_url.endswith(".gif")

    # Valid WebP
    webp_buf = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 255, 0)).save(webp_buf, format="WEBP")
    webp_url = validate_and_save_image(webp_buf.getvalue(), "test.webp", project_id="prj_test")
    assert webp_url.startswith("/projects/prj_test/media/")
    assert webp_url.endswith(".webp")

    # Dimensions exceed 4096px
    oversize_dim_buf = io.BytesIO()
    Image.new("RGB", (4097, 10)).save(oversize_dim_buf, format="PNG")
    with pytest.raises(ValueError, match="exceed maximum allowed 4096x4096 pixels"):
        validate_and_save_image(oversize_dim_buf.getvalue(), "large.png")

    # Rejection of invalid types (e.g. executable, HTML, PDF)
    with pytest.raises(ValueError, match="Unsupported or invalid image format"):
        validate_and_save_image(b"MZ\x90\x00" + b"\x00" * 100, "malware.exe")

    with pytest.raises(ValueError, match="Unsupported or invalid image format"):
        validate_and_save_image(b"<script>alert(1)</script>", "exploit.html")

    # Rejection of oversize file (> 5 MB)
    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="exceeds maximum allowed size of 5 MB"):
        validate_and_save_image(oversize, "huge.png")


def test_cross_team_and_cross_event_negative(sub_db):
    """Verify cross-team and cross-event submission modifications are strictly rejected."""
    user_a = create_account(sub_db, email="alice@test.local", password="Password123!", name="Alice")
    user_b = create_account(sub_db, email="bob@test.local", password="Password123!", name="Bob")

    event_1 = create_event(
        sub_db,
        event_id="evt_01_neg",
        name="Event One",
        submissions_close=datetime.now(timezone.utc) + timedelta(days=10),
    )
    event_2 = create_event(
        sub_db,
        event_id="evt_02_neg",
        name="Event Two",
        submissions_close=datetime.now(timezone.utc) + timedelta(days=10),
    )
    track_1 = Track(id="trk_01_neg", event_id=event_1.id, name="Track 1")
    track_2 = Track(id="trk_02_neg", event_id=event_2.id, name="Track 2")
    sub_db.add_all([track_1, track_2])
    sub_db.commit()

    team_a = create_team(sub_db, event_id=event_1.id, name="Team Alpha", creator_account=user_a)
    team_b = create_team(sub_db, event_id=event_1.id, name="Team Beta", creator_account=user_b)

    # Alice creates Project Alpha
    proj_a = save_project_draft_or_submission(
        db=sub_db,
        event_id=event_1.id,
        team_id=team_a.id,
        account=user_a,
        title="Project Alpha",
        summary="Summary Alpha",
        track_id=track_1.id,
    )

    # Bob on Team B attempts to modify Alice's project_id
    with pytest.raises(ValueError, match="Project does not belong to your team"):
        save_project_draft_or_submission(
            db=sub_db,
            event_id=event_1.id,
            team_id=team_b.id,
            account=user_b,
            title="Hacked Project",
            summary="Hacked Summary",
            track_id=track_1.id,
            project_id=proj_a.id,
        )

    # Cross-event mismatch: team_a belongs to event_1, but submission targets event_2
    with pytest.raises(ValueError, match="Invalid team for this event"):
        save_project_draft_or_submission(
            db=sub_db,
            event_id=event_2.id,
            team_id=team_a.id,
            account=user_a,
            title="Cross Event Proj",
            summary="Invalid event",
            track_id=track_2.id,
        )


def test_optimistic_concurrency_conflict_sqlite(sub_db):
    """Verify optimistic concurrency control rejects conflicting/stale version updates."""
    from app.services import ConcurrencyConflictError

    user = create_account(sub_db, email="concurrent@test.local", password="Password123!", name="Concurrent Dev")
    event = create_event(
        sub_db,
        event_id="evt_occ_test",
        name="OCC Event",
        submissions_close=datetime.now(timezone.utc) + timedelta(days=10),
    )
    track = Track(id="trk_occ", event_id=event.id, name="OCC Track")
    sub_db.add(track)
    sub_db.commit()

    team = create_team(sub_db, event_id=event.id, name="OCC Team", creator_account=user)

    # Initial draft creation -> version 1
    p1 = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Original Title",
        summary="Original Summary",
        track_id=track.id,
    )
    assert p1.version == 1

    # Client A updates with expected_version = 1 -> succeeds, version becomes 2
    p2 = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Title by Client A",
        summary="Summary by Client A",
        track_id=track.id,
        project_id=p1.id,
        expected_version=1,
    )
    assert p2.version == 2
    assert p2.title == "Title by Client A"

    # Client B concurrently submits with stale expected_version = 1 -> raises ConcurrencyConflictError
    with pytest.raises(ConcurrencyConflictError, match="modified by another teammate"):
        save_project_draft_or_submission(
            db=sub_db,
            event_id=event.id,
            team_id=team.id,
            account=user,
            title="Title by Client B (Stale)",
            summary="Summary by Client B",
            track_id=track.id,
            project_id=p1.id,
            expected_version=1,
        )


def test_custom_questions_validation(sub_db):
    """Verify required custom event questions are enforced on final submission."""
    from app.models import EventQuestion

    user = create_account(sub_db, email="customq@test.local", password="Password123!", name="Question Dev")
    event = create_event(
        sub_db,
        event_id="evt_questions",
        name="Question Event",
        submissions_close=datetime.now(timezone.utc) + timedelta(days=10),
    )
    q1 = EventQuestion(
        id="q_arch_1",
        event_id=event.id,
        label="Architecture Overview",
        question_type="text",
        required=True,
        order_idx=1,
    )
    sub_db.add(q1)
    track = Track(id="trk_q", event_id=event.id, name="Q Track")
    sub_db.add(track)
    sub_db.commit()

    team = create_team(sub_db, event_id=event.id, name="Question Team", creator_account=user)

    # 1. Draft submission without answering required question -> succeeds
    draft = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Draft Title",
        summary="Draft Summary",
        track_id=track.id,
        is_final_submission=False,
    )
    assert draft.status == "draft"

    # 2. Final submission without answering required question -> raises ValueError
    with pytest.raises(ValueError, match="Required question 'Architecture Overview' must be answered"):
        save_project_draft_or_submission(
            db=sub_db,
            event_id=event.id,
            team_id=team.id,
            account=user,
            title="Draft Title",
            summary="Draft Summary",
            track_id=track.id,
            is_final_submission=True,
            project_id=draft.id,
            expected_version=1,
            answers={},
        )

    # 3. Final submission with answer -> succeeds
    submitted = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Draft Title",
        summary="Draft Summary",
        track_id=track.id,
        is_final_submission=True,
        project_id=draft.id,
        expected_version=1,
        answers={"q_arch_1": "Serverless AWS Lambda & PostgreSQL"},
    )
    assert submitted.status == "submitted"
    assert submitted.answers["q_arch_1"] == "Serverless AWS Lambda & PostgreSQL"


def test_mandatory_expected_version_on_project_update(sub_db):
    """Verify that updating an existing project without expected_version strictly raises ConcurrencyConflictError."""
    from app.services import ConcurrencyConflictError

    user = create_account(sub_db, email="mand_occ@test.local", password="Password123!", name="Mand Dev")
    event = create_event(
        sub_db,
        event_id="evt_mand_occ",
        name="Mandatory OCC Event",
        submissions_close=datetime.now(timezone.utc) + timedelta(days=5),
    )
    track = Track(id="trk_mand", event_id=event.id, name="Mand Track")
    sub_db.add(track)
    sub_db.commit()

    team = create_team(sub_db, event_id=event.id, name="Mand Team", creator_account=user)
    p = save_project_draft_or_submission(
        db=sub_db,
        event_id=event.id,
        team_id=team.id,
        account=user,
        title="Original",
        summary="Original Summary",
        track_id=track.id,
    )
    assert p.version == 1

    # Attempt update without expected_version (None) -> must raise ConcurrencyConflictError
    with pytest.raises(ConcurrencyConflictError, match="'expected_version' is required"):
        save_project_draft_or_submission(
            db=sub_db,
            event_id=event.id,
            team_id=team.id,
            account=user,
            title="Updated Without Version",
            summary="Attempted update",
            track_id=track.id,
            project_id=p.id,
            expected_version=None,
        )
