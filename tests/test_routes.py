from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.database import Base, get_db
from app.main import app
from app.models import Project
from app.seed import FIXTURES_PATH, seed


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session, path=FIXTURES_PATH)
        session.commit()

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()


def test_root_redirects_to_projects(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/projects"


def test_gallery_renders_known_fixture_titles(client):
    response = client.get("/projects")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Amber Hours" in response.text
    assert "Glass Signal" in response.text
    assert "Dry Harbour" in response.text
    assert "41 project records" in response.text


def test_gallery_search_case_insensitive(client):
    resp_lower = client.get("/projects?q=amber+hours")
    assert resp_lower.status_code == 200
    assert "Amber Hours" in resp_lower.text
    assert "Glass Signal" not in resp_lower.text

    resp_upper = client.get("/projects?q=AMBER")
    assert resp_upper.status_code == 200
    assert "Amber Hours" in resp_upper.text


def test_gallery_search_escapes_sql_wildcards(client):
    # Searching for '%' should not match all projects as a wildcard
    response = client.get("/projects?q=%")
    assert response.status_code == 200
    # No project has '%' in its title, so empty state should appear
    assert "No matching projects found" in response.text


def test_gallery_track_filtering(client):
    # Track trk_01 (Developer tools) has Dry Compass; Glass Signal is in trk_04 (Security)
    response = client.get("/projects?track=trk_01")
    assert response.status_code == 200
    assert "Dry Compass" in response.text
    assert "Glass Signal" not in response.text
    assert "Amber Hours" not in response.text


def test_gallery_combined_search_and_track_filter(client):
    # Amber Hours is in trk_05 (Climate)
    matched = client.get("/projects?q=amber&track=trk_05")
    assert matched.status_code == 200
    assert "Amber Hours" in matched.text

    mismatched = client.get("/projects?q=amber&track=trk_01")
    assert mismatched.status_code == 200
    assert "Amber Hours" not in mismatched.text
    assert "No matching projects found" in mismatched.text


def test_gallery_empty_search_results(client):
    response = client.get("/projects?q=definitely_not_a_real_project_12345")
    assert response.status_code == 200
    assert "No matching projects found" in response.text
    assert "Show all projects" in response.text


def test_project_detail_success(client):
    response = client.get("/projects/prj_01")
    assert response.status_code == 200
    assert "Glass Signal" in response.text
    assert "NorthKiln" in response.text
    assert "Security" in response.text
    assert "View source repository ↗" in response.text


def test_project_detail_not_found(client):
    response = client.get("/projects/nonexistent_prj_999", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert "Project not found" in response.text


def test_unsafe_repo_url_is_not_rendered_as_link():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session, path=FIXTURES_PATH)
        # Modify a project's repo_url to an unsafe scheme
        p = session.get(Project, "prj_01")
        p.repo_url = "javascript:alert(1)"
        session.commit()

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/projects/prj_01")
            assert resp.status_code == 200
            assert "javascript:alert(1)" not in resp.text
            assert "View source repository ↗" not in resp.text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_privacy_no_private_judging_data_leak(client):
    # Verify neither gallery nor detail leaks judge emails, reviewer comments, or scores
    gallery_resp = client.get("/projects")
    assert "tomas.varga@example.org" not in gallery_resp.text
    assert "ada@example.org" not in gallery_resp.text
    assert "Runs clean." not in gallery_resp.text
    assert "criteria" not in gallery_resp.text

    detail_resp = client.get("/projects/prj_01")
    assert "tomas.varga@example.org" not in detail_resp.text
    assert "ada@example.org" not in detail_resp.text
    assert "Runs clean." not in detail_resp.text
    assert "criteria" not in detail_resp.text


def test_security_headers_present(client):
    response = client.get("/projects")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
