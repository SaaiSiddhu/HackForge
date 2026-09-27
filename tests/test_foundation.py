import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.config import clear_settings_cache
from app.database import Base, get_db
from app.main import app
from app.models import Event, Project, Review
from app.seed import FIXTURES, seed


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "testing")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    clear_settings_cache()

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
        yield session
    engine.dispose()
    clear_settings_cache()


def test_seed_preserves_all_records_and_local_edits(db):
    data = json.loads(FIXTURES.read_text())
    db.get(Project, "prj_01").summary = "Local edit"
    db.commit()
    seed(db)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Project)) == len(data["projects"])
    assert db.scalar(select(func.count()).select_from(Review)) == len(data["scores"])
    assert db.get(Project, "prj_01").summary == "Local edit"
    assert db.get(Event, "evt_01").submissions_close.year == 2026
    assert db.get(Event, "evt_01").submissions_close.month == 3


def test_gallery_filters_and_does_not_leak_private_data(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            result = client.get("/projects")
            assert result.status_code == 200
            assert "Glass Signal" in result.text
            assert "tomas.varga@example.org" not in result.text
            assert "Runs clean." not in result.text
            assert "Glass Signal" in client.get("/projects?q=glass+signal").text
            assert "Glass Signal" not in client.get("/projects?track=trk_01").text
            assert client.get("/projects/missing").status_code == 404
            assert client.get("/projects/prj_01").status_code == 200
            assert client.get("/health").json()["status"] == "ok"
    finally:
        app.dependency_overrides.clear()
