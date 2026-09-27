import logging
from pathlib import Path
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .config import get_settings
from .database import check_database_health, get_db
from .models import Project, Team, Track

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

app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add baseline security headers to all HTTP responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
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
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail if isinstance(exc.detail, str) else "An error occurred"},
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
    db: Session = Depends(get_db),
):
    """Public searchable project gallery with track filtering and deterministic sorting."""
    stmt = (
        select(
            Project,
            Track.name.label("track_name"),
            Team.name.label("team_name"),
        )
        .join(Track, Project.track_id == Track.id)
        .join(Team, Project.team_id == Team.id)
    )

    clean_q = q.strip()
    if clean_q:
        escaped_q = escape_like_pattern(clean_q.lower())
        stmt = stmt.where(func.lower(Project.title).like(f"%{escaped_q}%", escape="\\"))

    clean_track = track.strip()
    if clean_track:
        stmt = stmt.where(Project.track_id == clean_track)

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
        },
    )


@app.get("/projects/{project_id}", summary="Public project detail")
def project_detail(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public project detail view. Never leaks private judging, reviewer, or email data."""
    project = db.get(Project, project_id)
    if project is None:
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
        },
    )
