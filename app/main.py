from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from .database import get_db
from .models import Project, Track, Team

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="HackForge", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "hackforge"}


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/projects", status_code=302)


@app.get("/projects")
def gallery(request: Request, q: str = Query("", max_length=200), track: str = "", db: Session = Depends(get_db)):
    stmt = select(Project, Track.name.label("track_name"), Team.name.label("team_name")).join(Track).join(Team)
    if q.strip():
        stmt = stmt.where(func.lower(Project.title).contains(q.strip().lower(), autoescape=True))
    if track:
        stmt = stmt.where(Project.track_id == track)
    rows = db.execute(stmt.order_by(Project.title, Project.id)).all()
    return templates.TemplateResponse(request=request, name="gallery.html", context={
        "rows": rows, "tracks": db.scalars(select(Track).order_by(Track.name)).all(), "q": q, "track": track})


@app.get("/projects/{project_id}")
def project_detail(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    safe_repo = project.repo_url if project.repo_url.startswith(("https://", "http://")) else None
    return templates.TemplateResponse(request=request, name="project.html", context={
        "project": project, "repo_url": safe_repo, "team": db.get(Team, project.team_id), "track": db.get(Track, project.track_id)})
