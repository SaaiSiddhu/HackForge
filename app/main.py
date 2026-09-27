import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .auth import (
    clear_session_cookie,
    create_session,
    generate_csrf_token,
    get_current_account,
    require_authenticated_account,
    require_event_organizer,
    revoke_session,
    set_csrf_cookie,
    set_session_cookie,
    validate_csrf,
)
from .config import get_settings
from .database import check_database_health, get_db
from .models import (
    Account,
    Event,
    EventAuditLog,
    EventRole,
    Project,
    Team,
    TeamInvitation,
    TeamMembership,
    Track,
    EventQuestion,
)
from .services import (
    ConcurrencyConflictError,
    accept_team_invitation,
    add_event_prize,
    authenticate_account,
    create_account,
    create_event,
    create_event_question,
    create_event_track,
    create_team,
    create_team_invitation,
    delete_event_prize,
    delete_event_question,
    delete_event_track,
    ensure_utc,
    get_account_team_for_event,
    revoke_team_invitation,
    save_project_draft_or_submission,
    update_event,
    update_project_thumbnail,
    validate_and_save_image,
    validate_and_save_image_with_path,
)

# Configure logging
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hackforge")

ROOT = Path(__file__).resolve().parent

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url=None,  # Disabled for clean public production baseline
    redoc_url=None,
)

# Static asset mounts (uploads are access-controlled via /projects/{id}/media/{filename})
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

from .templating import templates, get_request_csrf_token
from .routers.judging import router as judging_router

app.include_router(judging_router)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add baseline security headers and ensure a synchronized CSRF token per request."""
    # Ensure request.state has the token for this request lifecycle
    existing_cookie = request.cookies.get("csrf_token")
    if existing_cookie:
        request.state.csrf_token = existing_cookie
        must_set_cookie = False
    else:
        request.state.csrf_token = generate_csrf_token()
        must_set_cookie = True

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

    # Issue CSRF cookie if not already present on incoming request
    if must_set_cookie:
        set_csrf_cookie(response, request.state.csrf_token)

    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Clean HTTP exception handler with friendly HTML error responses for browsers."""
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "status_code": exc.status_code,
                "message": exc.detail if isinstance(exc.detail, str) else "An error occurred",
            },
            status_code=exc.status_code,
        )
    msg = exc.detail if isinstance(exc.detail, str) else "An error occurred"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": msg, "detail": msg},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all error handler that prevents leaking exception traces to clients."""
    logger.exception("Unhandled server exception: %s", exc)
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "status_code": 500,
                "message": "An unexpected server error occurred. Please try again later.",
            },
            status_code=500,
        )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},
    )




# --- Observability / Health Probes ---


@app.get("/health/live", summary="Liveness probe")
def liveness():
    """Verify application process is active and serving HTTP requests."""
    return {"status": "ok", "service": "hackforge"}


@app.get("/health/ready", summary="Readiness probe")
def readiness():
    """Verify application dependencies (database connectivity) are fully operational."""
    healthy = check_database_health()
    if not healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "service": "hackforge", "error": "database unavailable"},
        )
    return {"status": "ok", "service": "hackforge", "database": "connected"}


@app.get("/health", summary="Health check (readiness alias)")
def health():
    """Backward-compatible health endpoint checking database availability."""
    return readiness()


# --- Public Navigation & Gallery ---


@app.get("/", include_in_schema=False)
def home():
    """Redirect root path to the public projects gallery."""
    return RedirectResponse(url="/projects", status_code=status.HTTP_302_FOUND)


def escape_like_pattern(text: str) -> str:
    """Escape SQL LIKE wildcards % and _ so search input is treated as literal data."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/projects", summary="Public project gallery")
def gallery(
    request: Request,
    q: Annotated[str, Query(max_length=200)] = "",
    track: Annotated[str, Query(max_length=100)] = "",
    event: Annotated[str, Query(max_length=100)] = "",
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Public searchable project gallery showing submitted projects with track filtering."""
    stmt = (
        select(
            Project,
            Track.name.label("track_name"),
            Team.name.label("team_name"),
        )
        .join(Track, Project.track_id == Track.id)
        .join(Team, Project.team_id == Team.id)
        .where(Project.status == "submitted")
    )

    clean_q = q.strip()
    if clean_q:
        escaped_q = escape_like_pattern(clean_q.lower())
        stmt = stmt.where(func.lower(Project.title).like(f"%{escaped_q}%", escape="\\"))

    clean_track = track.strip()
    if clean_track:
        stmt = stmt.where(Project.track_id == clean_track)

    clean_event = event.strip()
    if clean_event:
        stmt = stmt.where(Project.event_id == clean_event)

    # Deterministic alphabetical ordering by title, then unique id
    stmt = stmt.order_by(func.lower(Project.title).asc(), Project.id.asc())
    rows = db.execute(stmt).all()
    tracks = db.scalars(select(Track).order_by(Track.name.asc())).all()

    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={
            "rows": rows,
            "tracks": tracks,
            "q": clean_q,
            "track": clean_track,
            "current_account": account,
            "csrf_token": get_request_csrf_token(request),
        },
    )


@app.get("/projects/{project_id}", summary="Public project detail")
def project_detail(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Public project detail view. Never leaks private judging, reviewer, or email data."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # If draft, only team members or platform admins can view
    if project.status == "draft":
        if not account:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        is_member = db.scalars(
            select(TeamMembership).where(
                TeamMembership.team_id == project.team_id,
                TeamMembership.account_id == account.id,
            )
        ).first() is not None
        if not is_member and not account.is_platform_admin:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    team = db.get(Team, project.team_id)
    track = db.get(Track, project.track_id)

    return templates.TemplateResponse(
        request=request,
        name="project.html",
        context={
            "project": project,
            "repo_url": project.safe_repo_url,
            "team": team,
            "track": track,
            "current_account": account,
            "csrf_token": get_request_csrf_token(request),
        },
    )


# --- Authentication Routes ---


@app.get("/login", summary="Login view")
def login_view(
    request: Request,
    account: Optional[Account] = Depends(get_current_account),
):
    if account:
        return RedirectResponse(url="/projects", status_code=status.HTTP_302_FOUND)
    csrf_token = get_request_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf_token, "current_account": None},
    )


@app.post("/login", summary="Login form submission")
def login_post(
    request: Request,
    response: Response,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token_form=csrf_token)
    account = authenticate_account(db, email=email, password=password)
    if not account:
        csrf = get_request_csrf_token(request)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Invalid email or password.",
                "email": email,
                "csrf_token": csrf,
                "current_account": None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    _, raw_token = create_session(db, account.id)
    redirect = RedirectResponse(url="/projects", status_code=status.HTTP_302_FOUND)
    set_session_cookie(redirect, raw_token)
    return redirect


@app.get("/register", summary="Registration view")
def register_view(
    request: Request,
    account: Optional[Account] = Depends(get_current_account),
):
    if account:
        return RedirectResponse(url="/projects", status_code=status.HTTP_302_FOUND)
    csrf_token = get_request_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"csrf_token": csrf_token, "current_account": None},
    )


@app.post("/register", summary="Registration form submission")
def register_post(
    request: Request,
    response: Response,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token_form=csrf_token)
    try:
        account = create_account(db, email=email, password=password, name=name)
        _, raw_token = create_session(db, account.id)
        redirect = RedirectResponse(url="/projects", status_code=status.HTTP_302_FOUND)
        set_session_cookie(redirect, raw_token)
        return redirect
    except ValueError as exc:
        csrf = get_request_csrf_token(request)
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": str(exc),
                "name": name,
                "email": email,
                "csrf_token": csrf,
                "current_account": None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.post("/logout", summary="Logout handler")
def logout(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token_form=csrf_token)
    token = request.cookies.get("session")
    if token:
        revoke_session(db, token)
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    clear_session_cookie(redirect)
    return redirect


# --- Event Routes ---


@app.get("/events/new", summary="Create new event form")
def new_event_view(
    request: Request,
    account: Account = Depends(require_authenticated_account),
):
    """Render event creation form."""
    csrf_token = get_request_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="event_new.html",
        context={"csrf_token": csrf_token, "current_account": account},
    )


@app.post("/events/new", summary="Create new event")
def new_event_post(
    request: Request,
    event_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
    submissions_close: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    registration_open: Annotated[str, Form()] = "",
    registration_close: Annotated[str, Form()] = "",
    submissions_open: Annotated[str, Form()] = "",
    min_team_size: Annotated[int, Form()] = 1,
    max_team_size: Annotated[int, Form()] = 5,
    tracks: Annotated[str, Form()] = "",
    prizes: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    """Validate and create a new hackathon event with tracks, prizes, and questions."""
    validate_csrf(request, csrf_token_form=csrf_token)

    def parse_dt(s: str) -> Optional[datetime]:
        s = s.strip()
        if not s:
            return None
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    try:
        close_dt = parse_dt(submissions_close)
        if not close_dt:
            raise ValueError("Submissions close date is required.")

        reg_open_dt = parse_dt(registration_open)
        reg_close_dt = parse_dt(registration_close)
        sub_open_dt = parse_dt(submissions_open)

        # Parse tracks
        track_list = []
        for t in tracks.replace("\r", "").split("\n"):
            t = t.strip()
            if t:
                track_list.append({"name": t})

        # Parse prizes
        prize_list = []
        if prizes.strip():
            try:
                import json
                prize_list = json.loads(prizes)
            except Exception:
                for line in prizes.replace("\r", "").split("\n"):
                    if line.strip():
                        prize_list.append({"title": line.strip()})

        ev = create_event(
            db,
            event_id=event_id,
            name=name,
            submissions_close=close_dt,
            description=description,
            registration_open=reg_open_dt,
            registration_close=reg_close_dt,
            submissions_open=sub_open_dt,
            min_team_size=min_team_size,
            max_team_size=max_team_size,
            tracks=track_list,
            prizes=prize_list,
            creator_account_id=account.id,
        )
        return RedirectResponse(url=f"/events/{ev.id}/manage", status_code=status.HTTP_302_FOUND)
    except Exception as exc:
        return templates.TemplateResponse(
            request=request,
            name="event_new.html",
            context={
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
                "values": {
                    "event_id": event_id,
                    "name": name,
                    "description": description,
                    "min_team_size": min_team_size,
                    "max_team_size": max_team_size,
                    "submissions_close": submissions_close,
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.get("/events", summary="List events")
def list_events(
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    events = db.scalars(select(Event).order_by(Event.submissions_close.desc())).all()
    now = datetime.now(timezone.utc)

    # Attach computed is_open attribute
    for ev in events:
        ev.is_open = ev.is_submissions_open(now)

    organizer_event_ids = set()
    if account:
        org_roles = db.scalars(
            select(EventRole.event_id).where(
                EventRole.account_id == account.id,
                EventRole.role == "organizer",
            )
        ).all()
        organizer_event_ids = set(org_roles)

    return templates.TemplateResponse(
        request=request,
        name="events.html",
        context={
            "events": events,
            "organizer_event_ids": organizer_event_ids,
            "current_account": account,
            "csrf_token": get_request_csrf_token(request),
        },
    )


@app.get("/events/{event_id}/manage", summary="Organizer event management view")
def manage_event_view(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
    for t in tracks:
        t.project_count = db.scalar(
            select(func.count(Project.id)).where(Project.track_id == t.id)
        ) or 0

    questions = db.scalars(
        select(EventQuestion)
        .where(EventQuestion.event_id == event_id)
        .order_by(EventQuestion.order_idx.asc())
    ).all()

    audit_logs = db.scalars(
        select(EventAuditLog)
        .where(EventAuditLog.event_id == event_id)
        .order_by(EventAuditLog.created_at.desc())
        .limit(20)
    ).all()

    from .judging_services import get_active_rubric, get_organizer_judging_progress
    from .models import Judge, JudgeInvitation

    rubric = get_active_rubric(db, event_id)
    judges = db.scalars(select(Judge).where(Judge.event_id == event_id)).all()
    invitations = db.scalars(
        select(JudgeInvitation).where(JudgeInvitation.event_id == event_id).order_by(JudgeInvitation.created_at.desc())
    ).all()
    judging_progress = get_organizer_judging_progress(db, event_id)

    csrf_token = get_request_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="event_manage.html",
        context={
            "event": event,
            "tracks": tracks,
            "questions": questions,
            "prizes": event.prizes or [],
            "rubric": rubric,
            "judges": judges,
            "invitations": invitations,
            "judging_progress": judging_progress,
            "audit_logs": audit_logs,
            "csrf_token": csrf_token,
            "current_account": account,
            "invited_token": request.query_params.get("invited"),
        },
    )


@app.post("/events/{event_id}/manage", summary="Organizer update event configuration")
def manage_event_post(
    event_id: str,
    request: Request,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    min_team_size: Annotated[int, Form()] = 1,
    max_team_size: Annotated[int, Form()] = 5,
    submissions_close: Annotated[str, Form()] = "",
    registration_open: Annotated[str, Form()] = "",
    registration_close: Annotated[str, Form()] = "",
    submissions_open: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    validate_csrf(request, csrf_token_form=csrf_token)

    def parse_dt(s: str) -> Optional[datetime]:
        s = s.strip()
        if not s:
            return None
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    try:
        close_dt = parse_dt(submissions_close)
        if not close_dt:
            raise ValueError("Submissions close date is required.")

        reg_open_dt = parse_dt(registration_open)
        reg_close_dt = parse_dt(registration_close)
        sub_open_dt = parse_dt(submissions_open)

        update_event(
            db,
            event_id=event_id,
            name=name,
            description=description,
            min_team_size=min_team_size,
            max_team_size=max_team_size,
            registration_open=reg_open_dt,
            registration_close=reg_close_dt,
            submissions_open=sub_open_dt,
            submissions_close=close_dt,
            actor_account_id=account.id,
        )
        return RedirectResponse(
            url=f"/events/{event_id}/manage",
            status_code=status.HTTP_302_FOUND,
        )
    except Exception as exc:
        event = db.get(Event, event_id)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        for t in tracks:
            t.project_count = db.scalar(
                select(func.count(Project.id)).where(Project.track_id == t.id)
            ) or 0
        questions = db.scalars(
            select(EventQuestion)
            .where(EventQuestion.event_id == event_id)
            .order_by(EventQuestion.order_idx.asc())
        ).all()
        audit_logs = db.scalars(
            select(EventAuditLog)
            .where(EventAuditLog.event_id == event_id)
            .order_by(EventAuditLog.created_at.desc())
            .limit(20)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="event_manage.html",
            context={
                "event": event,
                "tracks": tracks,
                "questions": questions,
                "prizes": event.prizes or [],
                "audit_logs": audit_logs,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# --- Organizer Tracks, Prizes & Questions Routes ---


@app.post("/events/{event_id}/tracks", summary="Organizer add track")
async def event_add_track(
    event_id: str,
    request: Request,
    csrf_token: Annotated[Optional[str], Form()] = None,
    name: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        validate_csrf(request, csrf_token_form=payload.get("csrf_token"))
        track_name = payload.get("name", "")
    else:
        validate_csrf(request, csrf_token_form=csrf_token)
        track_name = name or ""

    try:
        track = create_event_track(db, event_id=event_id, name=track_name, actor_account_id=account.id)
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_201_CREATED, content={"id": track.id, "name": track.name})
        return RedirectResponse(url=f"/events/{event_id}/manage", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        if "application/json" in content_type:
            raise HTTPException(status_code=400, detail=str(exc))
        event = db.get(Event, event_id)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        audit_logs = db.scalars(select(EventAuditLog).where(EventAuditLog.event_id == event_id).order_by(EventAuditLog.created_at.desc()).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="event_manage.html",
            context={
                "event": event,
                "tracks": tracks,
                "questions": questions,
                "prizes": event.prizes or [],
                "audit_logs": audit_logs,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.post("/events/{event_id}/tracks/{track_id}/delete", summary="Organizer delete track")
async def event_delete_track(
    event_id: str,
    track_id: str,
    request: Request,
    csrf_token: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        validate_csrf(request, csrf_token_form=payload.get("csrf_token"))
    else:
        validate_csrf(request, csrf_token_form=csrf_token)

    try:
        delete_event_track(db, event_id=event_id, track_id=track_id, actor_account_id=account.id)
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})
        return RedirectResponse(url=f"/events/{event_id}/manage", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        if "application/json" in content_type:
            raise HTTPException(status_code=400, detail=str(exc))
        event = db.get(Event, event_id)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        for t in tracks:
            t.project_count = db.scalar(select(func.count(Project.id)).where(Project.track_id == t.id)) or 0
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        audit_logs = db.scalars(select(EventAuditLog).where(EventAuditLog.event_id == event_id).order_by(EventAuditLog.created_at.desc()).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="event_manage.html",
            context={
                "event": event,
                "tracks": tracks,
                "questions": questions,
                "prizes": event.prizes or [],
                "audit_logs": audit_logs,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.post("/events/{event_id}/prizes", summary="Organizer add prize")
async def event_add_prize(
    event_id: str,
    request: Request,
    csrf_token: Annotated[Optional[str], Form()] = None,
    title: Annotated[Optional[str], Form()] = None,
    description: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        validate_csrf(request, csrf_token_form=payload.get("csrf_token"))
        p_title = payload.get("title", "")
        p_desc = payload.get("description", "")
        p_amount = payload.get("amount", "")
    else:
        validate_csrf(request, csrf_token_form=csrf_token)
        p_title = title or ""
        p_desc = description
        p_amount = amount

    try:
        prizes = add_event_prize(db, event_id=event_id, title=p_title, description=p_desc, amount=p_amount, actor_account_id=account.id)
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_201_CREATED, content={"prizes": prizes})
        return RedirectResponse(url=f"/events/{event_id}/manage", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        if "application/json" in content_type:
            raise HTTPException(status_code=400, detail=str(exc))
        event = db.get(Event, event_id)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        audit_logs = db.scalars(select(EventAuditLog).where(EventAuditLog.event_id == event_id).order_by(EventAuditLog.created_at.desc()).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="event_manage.html",
            context={
                "event": event,
                "tracks": tracks,
                "questions": questions,
                "prizes": event.prizes or [],
                "audit_logs": audit_logs,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.post("/events/{event_id}/prizes/{prize_id_or_idx}/delete", summary="Organizer delete prize")
async def event_delete_prize(
    event_id: str,
    prize_id_or_idx: str,
    request: Request,
    csrf_token: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        validate_csrf(request, csrf_token_form=payload.get("csrf_token"))
    else:
        validate_csrf(request, csrf_token_form=csrf_token)

    try:
        prizes = delete_event_prize(db, event_id=event_id, prize_id_or_idx=prize_id_or_idx, actor_account_id=account.id)
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_200_OK, content={"prizes": prizes})
        return RedirectResponse(url=f"/events/{event_id}/manage", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        if "application/json" in content_type:
            raise HTTPException(status_code=400, detail=str(exc))
        event = db.get(Event, event_id)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        audit_logs = db.scalars(select(EventAuditLog).where(EventAuditLog.event_id == event_id).order_by(EventAuditLog.created_at.desc()).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="event_manage.html",
            context={
                "event": event,
                "tracks": tracks,
                "questions": questions,
                "prizes": event.prizes or [],
                "audit_logs": audit_logs,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.post("/events/{event_id}/questions", summary="Organizer add custom question")
async def event_add_question(
    event_id: str,
    request: Request,
    csrf_token: Annotated[Optional[str], Form()] = None,
    label: Annotated[Optional[str], Form()] = None,
    question_type: Annotated[str, Form()] = "text",
    required: Annotated[Optional[str], Form()] = None,
    options: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        validate_csrf(request, csrf_token_form=payload.get("csrf_token"))
        q_label = payload.get("label", "")
        q_type = payload.get("question_type", "text")
        q_req = bool(payload.get("required", False))
        q_options = payload.get("options", [])
    else:
        validate_csrf(request, csrf_token_form=csrf_token)
        q_label = label or ""
        q_type = question_type
        q_req = required in ("true", "1", "on", "yes", True)
        q_options = [opt.strip() for opt in (options or "").split("\n") if opt.strip()]

    try:
        question = create_event_question(
            db,
            event_id=event_id,
            label=q_label,
            question_type=q_type,
            required=q_req,
            options=q_options,
            actor_account_id=account.id,
        )
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_201_CREATED, content={"id": question.id, "label": question.label, "required": question.required})
        return RedirectResponse(url=f"/events/{event_id}/manage", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        if "application/json" in content_type:
            raise HTTPException(status_code=400, detail=str(exc))
        event = db.get(Event, event_id)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        audit_logs = db.scalars(select(EventAuditLog).where(EventAuditLog.event_id == event_id).order_by(EventAuditLog.created_at.desc()).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="event_manage.html",
            context={
                "event": event,
                "tracks": tracks,
                "questions": questions,
                "prizes": event.prizes or [],
                "audit_logs": audit_logs,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.post("/events/{event_id}/questions/{question_id}/delete", summary="Organizer delete custom question")
async def event_delete_question(
    event_id: str,
    question_id: str,
    request: Request,
    csrf_token: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    account: Account = Depends(require_event_organizer),
):
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        validate_csrf(request, csrf_token_form=payload.get("csrf_token"))
    else:
        validate_csrf(request, csrf_token_form=csrf_token)

    try:
        delete_event_question(db, event_id=event_id, question_id=question_id, actor_account_id=account.id)
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})
        return RedirectResponse(url=f"/events/{event_id}/manage", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        if "application/json" in content_type:
            raise HTTPException(status_code=400, detail=str(exc))
        event = db.get(Event, event_id)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        audit_logs = db.scalars(select(EventAuditLog).where(EventAuditLog.event_id == event_id).order_by(EventAuditLog.created_at.desc()).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="event_manage.html",
            context={
                "event": event,
                "tracks": tracks,
                "questions": questions,
                "prizes": event.prizes or [],
                "audit_logs": audit_logs,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# --- Team Routes ---


@app.get("/events/{event_id}/teams/new", summary="Form a new team view")
def team_new_view(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # If user already on a team in this event, redirect to that team
    existing_team = get_account_team_for_event(db, event_id, account.id)
    if existing_team:
        return RedirectResponse(url=f"/teams/{existing_team.id}", status_code=status.HTTP_302_FOUND)

    csrf_token = get_request_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="team_new.html",
        context={"event": event, "csrf_token": csrf_token, "current_account": account},
    )


@app.post("/events/{event_id}/teams/new", summary="Form a new team post")
def team_new_post(
    event_id: str,
    request: Request,
    name: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    validate_csrf(request, csrf_token_form=csrf_token)
    try:
        team = create_team(db, event_id=event_id, name=name, creator_account=account)
        return RedirectResponse(url=f"/teams/{team.id}", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        event = db.get(Event, event_id)
        return templates.TemplateResponse(
            request=request,
            name="team_new.html",
            context={
                "event": event,
                "error": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.get("/teams/{team_id}", summary="Team roster and management view")
def team_view(
    team_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    event = db.get(Event, team.event_id)
    members = db.scalars(
        select(TeamMembership).where(TeamMembership.team_id == team_id)
    ).all()
    invitations = db.scalars(
        select(TeamInvitation).where(TeamInvitation.team_id == team_id)
    ).all()

    is_captain = team.captain_account_id == account.id or account.is_platform_admin

    return templates.TemplateResponse(
        request=request,
        name="team.html",
        context={
            "team": team,
            "event": event,
            "members": members,
            "invitations": invitations,
            "is_captain": is_captain,
            "csrf_token": get_request_csrf_token(request),
            "current_account": account,
        },
    )


@app.post("/teams/{team_id}/invite", summary="Send team invitation")
def team_invite_post(
    team_id: str,
    request: Request,
    email: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    validate_csrf(request, csrf_token_form=csrf_token)
    try:
        create_team_invitation(db, team_id=team_id, email=email, requester=account)
        return RedirectResponse(url=f"/teams/{team_id}", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        team = db.get(Team, team_id)
        event = db.get(Event, team.event_id)
        members = db.scalars(select(TeamMembership).where(TeamMembership.team_id == team_id)).all()
        invitations = db.scalars(select(TeamInvitation).where(TeamInvitation.team_id == team_id)).all()
        return templates.TemplateResponse(
            request=request,
            name="team.html",
            context={
                "team": team,
                "event": event,
                "members": members,
                "invitations": invitations,
                "is_captain": True,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.get("/invitations/{token}", summary="View team invitation")
def invitation_view(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    invitation = db.scalars(select(TeamInvitation).where(TeamInvitation.token == token)).first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found or invalid.")

    team = db.get(Team, invitation.team_id)
    event = db.get(Event, team.event_id)

    return templates.TemplateResponse(
        request=request,
        name="invitation.html",
        context={
            "invitation": invitation,
            "team": team,
            "event": event,
            "csrf_token": get_request_csrf_token(request),
            "current_account": account,
        },
    )


@app.post("/invitations/{token}/accept", summary="Accept team invitation")
def invitation_accept_post(
    token: str,
    request: Request,
    csrf_token: Annotated[str, Form()],
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    validate_csrf(request, csrf_token_form=csrf_token)
    try:
        membership = accept_team_invitation(db, token=token, account=account)
        return RedirectResponse(url=f"/teams/{membership.team_id}", status_code=status.HTTP_302_FOUND)
    except ValueError as exc:
        invitation = db.scalars(select(TeamInvitation).where(TeamInvitation.token == token)).first()
        team = db.get(Team, invitation.team_id) if invitation else None
        event = db.get(Event, team.event_id) if team else None
        return templates.TemplateResponse(
            request=request,
            name="invitation.html",
            context={
                "invitation": invitation,
                "team": team,
                "event": event,
                "error": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# --- Project Submission Lifecycle Routes ---


@app.get("/events/{event_id}/submit", summary="Project submission & draft page")
def submit_view(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    team = get_account_team_for_event(db, event_id, account.id)
    if not team:
        # Prompt user to form or join a team first
        return RedirectResponse(url=f"/events/{event_id}/teams/new", status_code=status.HTTP_302_FOUND)

    tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
    questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
    project = db.scalars(
        select(Project).where(
            Project.event_id == event_id,
            Project.team_id == team.id,
        )
    ).first()

    now = datetime.now(timezone.utc)
    event_is_open = event.is_submissions_open(now)

    return templates.TemplateResponse(
        request=request,
        name="submit.html",
        context={
            "event": event,
            "team": team,
            "tracks": tracks,
            "questions": questions,
            "project": project,
            "event_is_open": event_is_open,
            "csrf_token": get_request_csrf_token(request),
            "current_account": account,
        },
    )


@app.post("/events/{event_id}/submit", summary="Submit or save project draft")
async def submit_post(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    now = datetime.now(timezone.utc)

    # 1. Deadline Check (Applies to both API probes and browser forms)
    if not event.is_submissions_open(now):
        s_close = ensure_utc(event.submissions_close)
        detail = (
            f"Submissions for event '{event.name}' closed on {s_close.isoformat()}."
            if s_close and now >= s_close
            else f"Submissions for event '{event.name}' are not open yet."
        )
        if "application/json" in request.headers.get("content-type", "").lower():
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"error": detail, "event_id": event_id, "submissions_close": s_close.isoformat() if s_close else None},
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    # Authentication check
    if not account:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    # 2. Check JSON payload (e.g. from automated checker probes or API)
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        csrf_candidate = payload.get("csrf_token") if isinstance(payload, dict) else None
        validate_csrf(request, csrf_token_form=csrf_candidate)
        team = get_account_team_for_event(db, event_id, account.id)
        if not team:
            raise HTTPException(status_code=400, detail="User does not belong to a team for this event.")

        expected_ver = payload.get("version") or payload.get("expected_version")
        try:
            saved_proj = save_project_draft_or_submission(
                db=db,
                event_id=event_id,
                team_id=team.id,
                account=account,
                title=payload.get("title", ""),
                summary=payload.get("summary", ""),
                repo_url=payload.get("repo_url", "https://github.com/example/hackforge-submission"),
                track_id=payload.get("track_id"),
                answers=payload.get("answers"),
                is_final_submission=True,
                expected_version=int(expected_ver) if expected_ver is not None else None,
            )
            return JSONResponse(
                status_code=status.HTTP_201_CREATED,
                content={"status": "ok", "project_id": saved_proj.id, "title": saved_proj.title},
            )
        except ConcurrencyConflictError as cce:
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(cce)})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # 3. Multipart / form payload from browser
    form = await request.form()
    csrf_token = form.get("csrf_token")
    validate_csrf(request, csrf_token_form=csrf_token)

    team = get_account_team_for_event(db, event_id, account.id)
    if not team:
        return RedirectResponse(url=f"/events/{event_id}/teams/new", status_code=status.HTTP_302_FOUND)

    title = str(form.get("title", ""))
    tagline = str(form.get("tagline", ""))
    track_id = str(form.get("track_id", ""))
    summary = str(form.get("summary", ""))
    repo_url = str(form.get("repo_url", ""))
    demo_url = str(form.get("demo_url", ""))
    description = str(form.get("description", ""))
    action = str(form.get("action", "draft"))  # "draft" or "submit"
    is_final = action == "submit"
    project_id = str(form.get("project_id", "")).strip() or None
    expected_ver_raw = form.get("expected_version") or form.get("version")
    expected_version = int(expected_ver_raw) if expected_ver_raw and str(expected_ver_raw).isdigit() else None

    # Collect answers to custom event questions
    answers = {}
    for key, val in form.items():
        if key.startswith("question_"):
            qid = key[len("question_"):]
            answers[qid] = str(val)

    # Handle image upload if present with bounded reading
    thumbnail_url = None
    target_disk_path = None
    new_project_id = None
    upload_file = form.get("image")
    if upload_file and hasattr(upload_file, "read"):
        chunks = []
        tot = 0
        while True:
            chunk = await upload_file.read(64 * 1024)
            if not chunk:
                break
            tot += len(chunk)
            if tot > 5 * 1024 * 1024:
                tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
                questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
                project = db.scalars(select(Project).where(Project.event_id == event_id, Project.team_id == team.id)).first()
                return templates.TemplateResponse(
                    request=request,
                    name="submit.html",
                    context={
                        "event": event,
                        "team": team,
                        "tracks": tracks,
                        "questions": questions,
                        "project": project,
                        "event_is_open": True,
                        "error_msg": "Image upload error: File size exceeds maximum allowed size of 5 MB.",
                        "csrf_token": get_request_csrf_token(request),
                        "current_account": account,
                    },
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            chunks.append(chunk)

        file_bytes = b"".join(chunks)
        if file_bytes:
            try:
                existing_p = db.scalars(select(Project).where(Project.event_id == event_id, Project.team_id == team.id)).first()
                if existing_p:
                    pid_for_media = existing_p.id
                elif project_id:
                    pid_for_media = project_id
                else:
                    new_project_id = f"prj_{uuid.uuid4().hex[:10]}"
                    pid_for_media = new_project_id
                thumbnail_url, target_disk_path = validate_and_save_image_with_path(file_bytes, getattr(upload_file, "filename", ""), project_id=pid_for_media)
            except ValueError as exc:
                tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
                questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
                project = db.scalars(select(Project).where(Project.event_id == event_id, Project.team_id == team.id)).first()
                return templates.TemplateResponse(
                    request=request,
                    name="submit.html",
                    context={
                        "event": event,
                        "team": team,
                        "tracks": tracks,
                        "questions": questions,
                        "project": project,
                        "event_is_open": True,
                        "error_msg": f"Image upload error: {exc}",
                        "csrf_token": get_request_csrf_token(request),
                        "current_account": account,
                    },
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

    try:
        saved_proj = save_project_draft_or_submission(
            db=db,
            event_id=event_id,
            team_id=team.id,
            account=account,
            title=title,
            summary=summary,
            repo_url=repo_url,
            track_id=track_id,
            tagline=tagline,
            description=description,
            demo_url=demo_url,
            thumbnail_url=thumbnail_url,
            answers=answers,
            is_final_submission=is_final,
            project_id=project_id,
            expected_version=expected_version,
            new_project_id=new_project_id,
        )
        if is_final:
            return RedirectResponse(url=f"/projects/{saved_proj.id}", status_code=status.HTTP_302_FOUND)
        else:
            return RedirectResponse(url=f"/events/{event_id}/submit", status_code=status.HTTP_302_FOUND)
    except ConcurrencyConflictError as cce:
        if target_disk_path and target_disk_path.exists():
            target_disk_path.unlink(missing_ok=True)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        project = db.scalars(select(Project).where(Project.event_id == event_id, Project.team_id == team.id)).first()
        return templates.TemplateResponse(
            request=request,
            name="submit.html",
            context={
                "event": event,
                "team": team,
                "tracks": tracks,
                "questions": questions,
                "project": project,
                "event_is_open": True,
                "error_msg": str(cce),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        if target_disk_path and target_disk_path.exists():
            target_disk_path.unlink(missing_ok=True)
        tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()
        questions = db.scalars(select(EventQuestion).where(EventQuestion.event_id == event_id).order_by(EventQuestion.order_idx.asc())).all()
        project = db.scalars(select(Project).where(Project.event_id == event_id, Project.team_id == team.id)).first()
        return templates.TemplateResponse(
            request=request,
            name="submit.html",
            context={
                "event": event,
                "team": team,
                "tracks": tracks,
                "questions": questions,
                "project": project,
                "event_is_open": True,
                "error_msg": str(exc),
                "csrf_token": get_request_csrf_token(request),
                "current_account": account,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@app.get("/projects/{project_id}/media/{filename}", summary="Access-controlled media delivery")
def project_media_download(
    project_id: str,
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Serve project media with access control for draft projects."""
    if project_id != "general":
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        if project.status != "submitted":
            if not account:
                raise HTTPException(status_code=401, detail="Authentication required to view draft media")

            is_member = db.scalars(
                select(TeamMembership).where(
                    TeamMembership.team_id == project.team_id,
                    TeamMembership.account_id == account.id,
                )
            ).first() is not None

            is_org = db.scalars(
                select(EventRole).where(
                    EventRole.event_id == project.event_id,
                    EventRole.account_id == account.id,
                    EventRole.role == "organizer",
                )
            ).first() is not None

            if not is_member and not is_org and not account.is_platform_admin:
                raise HTTPException(status_code=403, detail="Forbidden: You do not have access to this draft project media")

    safe_name = Path(filename).name
    if not safe_name or safe_name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    curr_settings = get_settings()
    file_path = (curr_settings.upload_dir / project_id / safe_name).resolve()
    try:
        file_path.relative_to(curr_settings.upload_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    ext = file_path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return FileResponse(file_path, media_type=media_types.get(ext, "application/octet-stream"))


@app.post("/projects/{project_id}/upload-image", summary="Upload project thumbnail or screenshot")
async def project_upload_image(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    csrf_token: Annotated[Optional[str], Form()] = None,
    expected_version: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Validate image with CSRF, deadline check, team authorization, bounded read, Pillow validation, and atomic OCC."""
    if not account:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    header_csrf = request.headers.get("x-csrf-token")
    validate_csrf(request, csrf_token_form=csrf_token or header_csrf)

    # Read expected_version from form, header, or query
    exp_ver_val = (
        expected_version
        or request.headers.get("x-expected-version")
        or request.query_params.get("expected_version")
    )
    parsed_expected_ver = None
    if exp_ver_val is not None and str(exp_ver_val).strip():
        try:
            parsed_expected_ver = int(str(exp_ver_val).strip())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Invalid expected_version; must be an integer.",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflict: 'expected_version' is required for project thumbnail updates.",
        )

    # Bounded read in chunks up to 5 MB
    chunks = []
    tot = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        tot += len(chunk)
        if tot > 5 * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File size exceeds the 5 MB limit.",
            )
        chunks.append(chunk)

    file_bytes = b"".join(chunks)

    try:
        updated_proj, url = update_project_thumbnail(
            db=db,
            project_id=project_id,
            file_bytes=file_bytes,
            original_filename=file.filename or "",
            account=account,
            expected_version=parsed_expected_ver,
        )
        return {"status": "ok", "url": url, "version": updated_proj.version}
    except ConcurrencyConflictError as cce:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(cce))
    except PermissionError as pe:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(pe))
    except ValueError as ve:
        msg = str(ve).lower()
        if "closed" in msg or "not open" in msg or "deadline" in msg:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(ve))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@app.post("/teams/{team_id}/invitations/{invitation_id}/revoke", summary="Revoke pending team invitation")
def revoke_invitation_route(
    team_id: str,
    invitation_id: str,
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
    account: Account = Depends(require_authenticated_account),
):
    """Revoke a pending team invitation by team captain or platform admin."""
    validate_csrf(request, csrf_token_form=csrf_token)
    try:
        revoke_team_invitation(db, team_id=team_id, invitation_id=invitation_id, account=account)
        return RedirectResponse(url=f"/teams/{team_id}", status_code=status.HTTP_302_FOUND)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
