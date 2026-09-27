"""Jinja2 template configuration and helpers."""

from pathlib import Path
from fastapi import Request
from fastapi.templating import Jinja2Templates

from .auth import generate_csrf_token

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")


def get_request_csrf_token(request: Request) -> str:
    """Extract synchronized CSRF token for template binding."""
    token = getattr(request.state, "csrf_token", None)
    if not token:
        token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
        request.state.csrf_token = token
    return token


templates.env.globals["get_csrf_token"] = get_request_csrf_token
