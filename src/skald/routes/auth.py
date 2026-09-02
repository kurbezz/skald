import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from skald.auth import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, create_session_cookie
from skald.config import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = "", next: str = "/jobs"):
    settings = get_settings()
    if not settings.auth_username or not settings.auth_password:
        # Nothing to protect, so there's no reason to show a login page.
        return RedirectResponse(url="/jobs")

    return templates.TemplateResponse(
        request, "login.html", {"error": bool(error), "next": next}
    )


@router.post("/login")
async def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/jobs"),
):
    settings = get_settings()

    valid = secrets.compare_digest(username, settings.auth_username) and secrets.compare_digest(
        password, settings.auth_password
    )
    if not valid:
        return RedirectResponse(url=f"/login?error=1&next={next}", status_code=303)

    response = RedirectResponse(url=next or "/jobs", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_cookie(),
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
        # `secure` is intentionally omitted: this app is commonly self-hosted
        # behind plain HTTP on a LAN. Put it behind an HTTPS reverse proxy
        # and add `secure=True` here if you need cookie transport security.
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
