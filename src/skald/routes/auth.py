import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from skald.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_cookie,
    require_csrf,
    safe_next,
)
from skald.config import get_settings
from skald.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request, error: str = "", expired: str = "", next: str = "/jobs", username: str = ""
):
    settings = get_settings()
    if not settings.auth_username or not settings.auth_password:
        # Nothing to protect, so there's no reason to show a login page.
        return RedirectResponse(url="/jobs")

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": bool(error),
            "expired": bool(expired),
            "next": safe_next(next),
            "username": username[:200],
        },
    )


@router.post("/login")
async def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/jobs"),
):
    settings = get_settings()
    next = safe_next(next)

    valid = secrets.compare_digest(username, settings.auth_username) and secrets.compare_digest(
        password, settings.auth_password
    )
    if not valid:
        query = urlencode({"error": 1, "next": next, "username": username})
        return RedirectResponse(url=f"/login?{query}", status_code=303)

    response = RedirectResponse(url=next, status_code=303)
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


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
