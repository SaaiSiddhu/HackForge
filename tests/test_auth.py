import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.auth import (
    create_session,
    generate_csrf_token,
    hash_password,
    hash_session_token,
    revoke_session,
    validate_csrf,
    verify_password,
)
from app.database import Base
from app.models import Account, SessionModel
from app.services import create_account, authenticate_account
from fastapi import HTTPException


@pytest.fixture
def auth_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_password_hashing_argon2id():
    """Verify Argon2id password hashing and minimum length validation."""
    with pytest.raises(ValueError, match="at least 8 characters"):
        hash_password("short")

    pw = "SecurePassword123!"
    pw_hash = hash_password(pw)
    assert pw_hash.startswith("$argon2id$")
    assert verify_password(pw, pw_hash) is True
    assert verify_password("WrongPassword123!", pw_hash) is False
    assert verify_password("", pw_hash) is False


def test_session_sha256_hashing_and_revocation(auth_db):
    """Verify high-entropy session tokens are stored as SHA-256 hashes and can be revoked."""
    account = create_account(
        auth_db,
        email="user@test.local",
        password="Password123!",
        name="Test User",
    )

    session_obj, raw_token = create_session(auth_db, account.id)
    expected_hash = hash_session_token(raw_token)
    assert session_obj.token_hash == expected_hash
    assert session_obj.account_id == account.id

    # Verify session lookup in DB
    found = auth_db.get(SessionModel, session_obj.id)
    assert found is not None
    assert found.token_hash == expected_hash

    # Revoke session
    revoked = revoke_session(auth_db, raw_token)
    assert revoked is True
    assert auth_db.get(SessionModel, session_obj.id) is None

    # Revoking non-existent session
    assert revoke_session(auth_db, "nonexistent_token") is False


def test_authenticate_account_service(auth_db):
    """Verify authenticate_account with case-insensitive email normalization."""
    account = create_account(
        auth_db,
        email="CaseInsensitive@Example.Com",
        password="ValidPassword123!",
        name="Case Tester",
    )
    assert account.email == "caseinsensitive@example.com"

    # Authenticate with mixed-case email
    auth_ok = authenticate_account(auth_db, "CASEINSENSITIVE@example.com", "ValidPassword123!")
    assert auth_ok is not None
    assert auth_ok.id == account.id

    # Invalid password
    assert authenticate_account(auth_db, "caseinsensitive@example.com", "BadPassword") is None

    # Non-existent user
    assert authenticate_account(auth_db, "nobody@example.com", "ValidPassword123!") is None


def test_csrf_token_validation():
    """Verify CSRF token validation logic."""
    class DummyRequest:
        def __init__(self, method="POST", cookies=None, headers=None):
            self.method = method
            self.cookies = cookies or {}
            self.headers = headers or {}

    token = generate_csrf_token()

    # GET request is safe and bypasses CSRF check
    get_req = DummyRequest(method="GET")
    validate_csrf(get_req)

    # Valid POST request with matching token
    post_req_valid = DummyRequest(
        method="POST",
        cookies={"csrf_token": token},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    validate_csrf(post_req_valid, csrf_token_form=token)

    # Missing token fails
    post_req_missing = DummyRequest(
        method="POST",
        cookies={},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(HTTPException) as exc_info:
        validate_csrf(post_req_missing, csrf_token_form=None)
    assert exc_info.value.status_code == 403

    # Mismatched token fails
    post_req_mismatch = DummyRequest(
        method="POST",
        cookies={"csrf_token": token},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(HTTPException) as exc_info:
        validate_csrf(post_req_mismatch, csrf_token_form="different_token")
    assert exc_info.value.status_code == 403


@pytest.fixture
def auth_client():
    from starlette.testclient import TestClient
    from app.main import app
    from app.database import get_db, Base
    from app.seed import FIXTURES_PATH, seed
    from app.config import get_settings

    settings = get_settings()
    settings.enable_demo_provisioning = True

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
    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    engine.dispose()


def test_first_visit_csrf_and_protected_logout(auth_client):
    """Verify first-visit CSRF synchronization between cookie and template, and verify POST /logout."""
    import re
    # 1. Start with fresh client and no cookies
    auth_client.cookies.clear()
    assert len(auth_client.cookies) == 0

    # 2. Fetch /register
    get_resp = auth_client.get("/register")
    assert get_resp.status_code == 200

    cookie_csrf = auth_client.cookies.get("csrf_token")
    assert cookie_csrf is not None

    # Extract hidden token from rendered HTML
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text)
    assert match is not None, "csrf_token hidden input not found in /register HTML"
    html_csrf = match.group(1)

    # Rendered template token and response cookie MUST be identical
    assert html_csrf == cookie_csrf

    # 3. Submit valid registration with extracted token -> succeeds without 403
    reg_resp = auth_client.post(
        "/register",
        data={
            "name": "First Visit User",
            "email": "firstvisit@example.com",
            "password": "SecurePassword123!",
            "csrf_token": html_csrf,
        },
        follow_redirects=False,
    )
    assert reg_resp.status_code == 302
    assert "session" in auth_client.cookies

    # 4. Attempt submission with altered/missing CSRF -> 403 Forbidden
    bad_resp = auth_client.post(
        "/register",
        data={
            "name": "Attacker",
            "email": "attacker@example.com",
            "password": "SecurePassword123!",
            "csrf_token": "tampered_token_xyz",
        },
        follow_redirects=False,
    )
    assert bad_resp.status_code == 403

    # 5. GET /logout is rejected (405 Method Not Allowed)
    get_logout_resp = auth_client.get("/logout")
    assert get_logout_resp.status_code == 405

    # 6. POST /logout without CSRF is rejected (403 Forbidden)
    no_csrf_logout = auth_client.post("/logout", data={}, follow_redirects=False)
    assert no_csrf_logout.status_code == 403

    # 7. POST /logout with valid CSRF succeeds (302) and clears session cookie
    current_csrf = auth_client.cookies.get("csrf_token")
    logout_resp = auth_client.post(
        "/logout",
        data={"csrf_token": current_csrf},
        follow_redirects=False,
    )
    assert logout_resp.status_code == 302
    # Verify session cookie was deleted/expired
    set_cookie_headers = [h for k, h in logout_resp.headers.raw if k.lower() == b"set-cookie"]
    session_cleared = any(b"session=" in h and (b"Max-Age=0" in h or b"expires=" in h) for h in set_cookie_headers)
    assert session_cleared


def test_bearer_token_and_json_csrf_behavior(auth_client):
    """Verify JSON CSRF requirement for cookies, pure Bearer auth bypass, and ambiguous credential rejection."""
    # 1. Login with demo participant to get a session cookie
    login_get = auth_client.get("/login")
    csrf = auth_client.cookies.get("csrf_token")
    login_resp = auth_client.post(
        "/login",
        data={
            "email": "participant@hackforge.local",
            "password": "Password123!",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 302
    assert "session" in auth_client.cookies

    # A cookie-authenticated JSON mutation without CSRF is REJECTED with 403 Forbidden
    cookie_json_resp = auth_client.post(
        "/events/evt_open_demo/submit",
        json={
            "title": "CSRF Bypass Test",
            "summary": "Should be rejected because session cookie has no CSRF token",
        },
    )
    assert cookie_json_resp.status_code == 403

    # Clear cookies for a pure Bearer client
    auth_client.cookies.clear()

    # 2. Pure Bearer token authenticated JSON mutation succeeds without CSRF
    bearer_headers = {"Authorization": "Bearer prt_demo"}
    bearer_resp = auth_client.post(
        "/events/evt_open_demo/submit",
        headers=bearer_headers,
        json={
            "title": "Bearer Auth Project",
            "tagline": "Pure API submission",
            "summary": "Submitted via pure Bearer token without CSRF token",
            "repo_url": "https://github.com/example/bearer-project",
            "action": "draft",
        },
    )
    assert bearer_resp.status_code in (200, 201)

    # 3. Conflicting credentials: session cookie belonging to participant + conflicting Bearer token for organizer
    auth_client.cookies.set("session", "prt_demo")
    conflicting_headers = {"Authorization": "Bearer org_demo"}
    conflict_resp = auth_client.post(
        "/events/evt_open_demo/submit",
        headers=conflicting_headers,
        json={"title": "Ambiguous", "summary": "Ambiguous"},
    )
    assert conflict_resp.status_code == 400
    assert "Ambiguous authentication credentials" in conflict_resp.text
