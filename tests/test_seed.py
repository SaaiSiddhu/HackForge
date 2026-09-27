import copy
import json
from pathlib import Path
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.database import Base
from app.models import Event, Judge, Project, Review, Team, Track
from app.seed import FixtureValidationError, seed, validate_fixtures_data

FIXTURES_PATH = Path(__file__).resolve().parent.parent / "fixtures.json"


@pytest.fixture
def memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_seed_preserves_derived_counts_and_all_records(memory_db):
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    with memory_db.begin():
        seed(memory_db, path=FIXTURES_PATH)

    # Derived dynamic count assertions
    assert memory_db.scalar(select(func.count()).select_from(Event)) == 1
    assert memory_db.scalar(select(func.count()).select_from(Track)) == len(data["tracks"])
    assert memory_db.scalar(select(func.count()).select_from(Team)) == len(data["teams"])
    assert memory_db.scalar(select(func.count()).select_from(Judge)) == len(data["judges"])
    assert memory_db.scalar(select(func.count()).select_from(Project)) == len(data["projects"])
    assert memory_db.scalar(select(func.count()).select_from(Review)) == len(data["scores"])

    # Verify all project IDs are preserved
    expected_project_ids = {p["id"] for p in data["projects"]}
    actual_project_ids = set(memory_db.scalars(select(Project.id)).all())
    assert actual_project_ids == expected_project_ids

    # Verify deliberate duplicate submissions prj_07 and prj_41 exist with distinct IDs
    prj_07 = memory_db.get(Project, "prj_07")
    prj_41 = memory_db.get(Project, "prj_41")
    assert prj_07 is not None
    assert prj_41 is not None
    assert prj_07.title == "Dry Harbour"
    assert prj_41.title == "Dry Harbour"
    assert prj_07.team_id == prj_41.team_id
    assert prj_07.track_id == prj_41.track_id
    assert prj_07.submitted_at != prj_41.submitted_at


def test_seed_preserves_closed_deadline_and_constant_scores(memory_db):
    with memory_db.begin():
        seed(memory_db, path=FIXTURES_PATH)

    evt = memory_db.get(Event, "evt_01")
    assert evt.submissions_close.year == 2026
    assert evt.submissions_close.month == 3
    assert evt.submissions_close.day == 1
    assert evt.submissions_close.hour == 18
    assert evt.submissions_close.minute == 0

    # Constant score judge preserved
    jdg_07_reviews = memory_db.scalars(
        select(Review).where(Review.judge_id == "jdg_07")
    ).all()
    assert len(jdg_07_reviews) == 3
    for r in jdg_07_reviews:
        assert set(r.criteria.values()) == {4}


def test_reseed_is_idempotent_and_preserves_local_edits(memory_db):
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    with memory_db.begin():
        seed(memory_db, path=FIXTURES_PATH)

    # Make local legitimate edits
    prj = memory_db.get(Project, "prj_01")
    prj.summary = "Custom locally edited summary for project 01"
    memory_db.commit()

    # Second seed run
    with memory_db.begin():
        seed(memory_db, path=FIXTURES_PATH)

    # Record counts remain exactly identical (no duplicates created)
    assert memory_db.scalar(select(func.count()).select_from(Project)) == len(data["projects"])
    assert memory_db.scalar(select(func.count()).select_from(Review)) == len(data["scores"])

    # Local edit survived
    updated_prj = memory_db.get(Project, "prj_01")
    assert updated_prj.summary == "Custom locally edited summary for project 01"


def test_invalid_input_fails_validation_and_leaves_no_partial_writes(memory_db, tmp_path):
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    # Initial clean seed
    seed(memory_db, path=FIXTURES_PATH)
    memory_db.commit()
    initial_count = memory_db.scalar(select(func.count()).select_from(Project))

    # Create corrupted fixture referencing an unknown team
    corrupted_data = copy.deepcopy(data)
    corrupted_data["projects"].append({
        "id": "prj_invalid_999",
        "team": "tm_nonexistent",
        "track": "trk_01",
        "title": "Invalid Team Project",
        "summary": "Should fail validation",
        "repo_url": "https://example.org/invalid",
        "submitted_at": "2026-03-01T12:00:00Z",
    })
    bad_file = tmp_path / "corrupted_fixtures.json"
    bad_file.write_text(json.dumps(corrupted_data), encoding="utf-8")

    with pytest.raises(FixtureValidationError, match="references unknown team"):
        try:
            with memory_db.begin_nested():
                seed(memory_db, path=bad_file)
        except Exception:
            memory_db.rollback()
            raise

    # Verify no partial writes occurred
    assert memory_db.scalar(select(func.count()).select_from(Project)) == initial_count
    assert memory_db.get(Project, "prj_invalid_999") is None


def test_validation_detects_contradictory_duplicate_project_ids():
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    corrupted = copy.deepcopy(data)
    corrupted["projects"].append(copy.deepcopy(corrupted["projects"][0]))

    with pytest.raises(FixtureValidationError, match="Contradictory duplicate project ID"):
        validate_fixtures_data(corrupted)


def test_operator_reset_reloads_fresh_data(memory_db):
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    with memory_db.begin():
        seed(memory_db, path=FIXTURES_PATH)

    prj = memory_db.get(Project, "prj_01")
    prj.summary = "Locally modified"
    memory_db.commit()

    # Operator performs deliberate reset
    with memory_db.begin():
        seed(memory_db, path=FIXTURES_PATH, reset=True)

    # Data is restored to pristine fixture state
    reloaded_prj = memory_db.get(Project, "prj_01")
    assert reloaded_prj.summary == data["projects"][0]["summary"]
