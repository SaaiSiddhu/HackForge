import io
import uuid
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.seed import FIXTURES_PATH, seed


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated test client with in-memory database and temporary uploads directory."""
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_demo_provisioning", True)
    monkeypatch.setenv("ENABLE_DEMO_PROVISIONING", "true")

    isolated_upload_dir = tmp_path / "uploads"
    isolated_upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "upload_dir", isolated_upload_dir)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session, path=FIXTURES_PATH, seed_demo=True)
        session.commit()

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    engine.dispose()


def test_full_browser_journey_and_deadline_rejection(client):
    """End-to-end journey: Register -> Login -> Form Team -> Draft -> Protected Media -> Submit -> Public Media & Closed Event."""
    # 1. Fetch register page to obtain initial synchronized CSRF cookie
    get_resp = client.get("/register")
    assert get_resp.status_code == 200
    csrf = client.cookies.get("csrf_token")
    assert csrf is not None
    # Hidden input in HTML must match cookie exactly
    assert f'value="{csrf}"' in get_resp.text

    unique_email = f"journey_{uuid.uuid4().hex[:6]}@example.com"
    reg_resp = client.post(
        "/register",
        data={
            "name": "Journey Explorer",
            "email": unique_email,
            "password": "SecurePassword123!",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert reg_resp.status_code == 302
    assert "session" in client.cookies

    # 2. Form a team for open event
    csrf = client.cookies.get("csrf_token")
    team_resp = client.post(
        "/events/evt_open_demo/teams/new",
        data={
            "name": "Quantum Hackers",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert team_resp.status_code == 302
    team_redirect = team_resp.headers["location"]
    team_id = team_redirect.split("/")[-1]

    # 3. View team page
    team_page = client.get(f"/teams/{team_id}")
    assert team_page.status_code == 200
    assert "Quantum Hackers" in team_page.text

    # 4. Save project draft with real 1x1 valid PNG thumbnail
    img_buf = io.BytesIO()
    Image.new("RGBA", (1, 1), (0, 128, 255, 255)).save(img_buf, format="PNG")
    real_png_bytes = img_buf.getvalue()

    csrf = client.cookies.get("csrf_token")
    draft_resp = client.post(
        "/events/evt_open_demo/submit",
        data={
            "title": "Quantum AI Visualizer",
            "tagline": "Realtime quantum state rendering",
            "track_id": "trk_demo_01",
            "summary": "Visualizing complex qubits interactively.",
            "repo_url": "https://github.com/example/quantum-ai",
            "action": "draft",
            "csrf_token": csrf,
            "expected_version": 1,
        },
        files={"image": ("thumbnail.png", io.BytesIO(real_png_bytes), "image/png")},
        follow_redirects=False,
    )
    assert draft_resp.status_code == 302

    # 5. Public gallery does NOT show draft project
    gallery_resp = client.get("/projects")
    assert gallery_resp.status_code == 200
    assert "Quantum AI Visualizer" not in gallery_resp.text

    # 6. Check draft media route protection
    # Get submit view to discover thumbnail media URL
    submit_page = client.get("/events/evt_open_demo/submit")
    assert submit_page.status_code == 200
    assert "/media/" in submit_page.text

    # Extract media URL
    import re
    media_match = re.search(r'(/projects/[^/]+/media/[a-f0-9]+\.png)', submit_page.text)
    assert media_match is not None
    media_url = media_match.group(1)

    # Author can view own draft thumbnail
    author_media_resp = client.get(media_url)
    assert author_media_resp.status_code == 200
    assert author_media_resp.headers["content-type"] == "image/png"

    # Unauthenticated visitor CANNOT view draft media (access controlled)
    anon_client = TestClient(app)
    anon_media_resp = anon_client.get(media_url)
    assert anon_media_resp.status_code in (401, 403)

    # 7. Final submit project
    csrf = client.cookies.get("csrf_token")
    final_resp = client.post(
        "/events/evt_open_demo/submit",
        data={
            "title": "Quantum AI Visualizer",
            "tagline": "Realtime quantum state rendering",
            "track_id": "trk_demo_01",
            "summary": "Visualizing complex qubits interactively.",
            "repo_url": "https://github.com/example/quantum-ai",
            "action": "submit",
            "csrf_token": csrf,
            "expected_version": 1,
        },
        follow_redirects=False,
    )
    assert final_resp.status_code == 302

    # 8. Public gallery NOW shows submitted project!
    gallery_resp2 = client.get("/projects")
    assert gallery_resp2.status_code == 200
    assert "Quantum AI Visualizer" in gallery_resp2.text

    # Now that it's submitted, unauthenticated visitor CAN view the thumbnail publicly
    anon_media_resp2 = anon_client.get(media_url)
    assert anon_media_resp2.status_code == 200
    assert anon_media_resp2.headers["content-type"] == "image/png"

    # 9. Attempt submission against closed event evt_01 -> strictly refused (403)!
    csrf = client.cookies.get("csrf_token")
    closed_resp = client.post(
        "/events/evt_01/submit",
        json={"title": "Late Project", "summary": "Too late"},
        headers={"X-CSRF-Token": csrf},
    )
    assert closed_resp.status_code == 403
    assert "closed" in closed_resp.json()["error"].lower()
