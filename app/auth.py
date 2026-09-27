import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .database import get_db
from .models import Account, EventRole, SessionModel

ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hash password using Argon2id with strict length requirement."""
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    return ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against Argon2id hash."""
    if not password or not password_hash:
        return False
    try:
        return ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def generate_session_token() -> str:
    """Generate cryptographically secure 256-bit URL-safe token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Compute SHA-256 hash of session token for safe storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    db: Session,
    account_id: str,
    duration_days: int = 7,
    explicit_token: Optional[str] = None,
) -> tuple[SessionModel, str]:
    """Create a new session record in the database and return (session, raw_token)."""
    raw_token = explicit_token or generate_session_token()
    token_hash = hash_session_token(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=duration_days)

    session_id = f"ses_{secrets.token_hex(12)}"
    session_obj = SessionModel(
        id=session_id,
        token_hash=token_hash,
        account_id=account_id,
        expires_at=expires_at,
        created_at=now,
    )
    db.add(session_obj)
    db.commit()
    db.refresh(session_obj)
    return session_obj, raw_token


def revoke_session(db: Session, token: str) -> bool:
    """Revoke session by token."""
    if not token:
        return False
    token_hash = hash_session_token(token)
    stmt = select(SessionModel).where(SessionModel.token_hash == token_hash)
    session_obj = db.scalars(stmt).first()
    if session_obj:
        db.delete(session_obj)
        db.commit()
        return True
    return False


def set_session_cookie(response: Response, token: str, max_age: int = 7 * 86400) -> None:
    """Set secure, HttpOnly session cookie on response."""
    settings = get_settings()
    response.set_cookie(
        key="session",
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Clear session cookie from client."""
    response.delete_cookie(key="session", path="/")


# --- CSRF Protection ---


def generate_csrf_token() -> str:
    """Generate cryptographically secure CSRF token."""
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, csrf_token: str) -> None:
    """Set CSRF cookie for client/forms."""
    settings = get_settings()
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        max_age=7 * 86400,
        httponly=False,  # Accessible for form binding / double submit
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def validate_csrf(
    request: Request,
    csrf_token_form: Optional[str] = None,
) -> None:
    """Validate CSRF token for mutating requests.
    
    - Safe HTTP methods (GET, HEAD, OPTIONS) do not require CSRF tokens.
    - Pure Bearer token requests (Authorization: Bearer <token> without session cookie)
      bypass CSRF validation because Bearer tokens are not ambient credentials.
    - Any request authenticated via session cookies requires valid CSRF tokens,
      regardless of Content-Type (no blanket JSON exemption).
    - Requests presenting ambiguous credentials (both cookie and conflicting bearer token)
      are rejected.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    cookie_token = request.cookies.get("session")
    auth_header = request.headers.get("Authorization", "")
    bearer_token = None
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:].strip()

    # Reject ambiguous credentials
    if cookie_token and bearer_token and cookie_token != bearer_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ambiguous authentication credentials: both session cookie and conflicting Bearer token provided.",
        )

    # Pure Bearer authentication does not rely on ambient browser credentials
    if bearer_token and not cookie_token:
        return

    # Cookie-authenticated or browser form requests must validate CSRF
    csrf_cookie = request.cookies.get("csrf_token")
    token_from_header = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken")
    token_candidate = csrf_token_form or token_from_header

    if not csrf_cookie or not token_candidate:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid.",
        )

    if not secrets.compare_digest(csrf_cookie, token_candidate):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed.",
        )


# --- Authentication Dependencies ---


def get_current_account(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[Account]:
    """Retrieve currently authenticated account from session cookie or Bearer token."""
    cookie_token = request.cookies.get("session")
    auth_header = request.headers.get("Authorization", "")
    bearer_token = None
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:].strip()

    # Reject ambiguous credentials
    if cookie_token and bearer_token and cookie_token != bearer_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ambiguous authentication credentials: both session cookie and conflicting Bearer token provided.",
        )

    token = cookie_token or bearer_token
    if not token:
        return None

    token_hash = hash_session_token(token)
    stmt = (
        select(SessionModel)
        .where(SessionModel.token_hash == token_hash)
    )
    session_obj = db.scalars(stmt).first()

    if not session_obj:
        return None

    now = datetime.now(timezone.utc)
    expires_at = session_obj.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)

    if expires_at < now:
        db.delete(session_obj)
        db.commit()
        return None

    return session_obj.account


def require_authenticated_account(
    account: Optional[Account] = Depends(get_current_account),
) -> Account:
    """Require an authenticated user account."""
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return account


def require_platform_admin(
    account: Account = Depends(require_authenticated_account),
) -> Account:
    """Require platform administrator status."""
    if not account.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator privileges required",
        )
    return account


def has_event_role(db: Session, event_id: str, account_id: str, role: str) -> bool:
    """Check if an account possesses a specific role for an event."""
    stmt = select(EventRole).where(
        EventRole.event_id == event_id,
        EventRole.account_id == account_id,
        EventRole.role == role,
    )
    return db.scalars(stmt).first() is not None


def require_event_organizer(
    event_id: str,
    account: Account = Depends(require_authenticated_account),
    db: Session = Depends(get_db),
) -> Account:
    """Require organizer role for the specified event (or platform admin)."""
    if account.is_platform_admin or has_event_role(db, event_id, account.id, "organizer"):
        return account
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Organizer privileges required for this event",
    )
