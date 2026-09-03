import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException
from starlette.requests import HTTPConnection, Request

from skald.config import get_settings

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days


def create_csrf_token(session_cookie: str | None) -> str:
    """Create a stateless token bound to the browser's current session cookie."""
    session_binding = session_cookie or ""
    payload = f"csrf:{session_binding}"
    settings = get_settings()
    return hmac.new(
        settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_csrf_token(session_cookie: str | None, token: str | None) -> bool:
    """Return whether *token* is the current session's CSRF token."""
    if not isinstance(token, str) or not token:
        return False

    expected_token = create_csrf_token(session_cookie)
    return hmac.compare_digest(token, expected_token)


async def verify_csrf_form(request: Request) -> bool:
    """Verify a form carries exactly one CSRF token for this session."""
    tokens = (await request.form()).getlist("csrf_token")
    if len(tokens) != 1 or not isinstance(tokens[0], str):
        return False

    return verify_csrf_token(request.cookies.get(SESSION_COOKIE_NAME), tokens[0])


def create_session_cookie() -> str:
    settings = get_settings()
    payload = f"authenticated:{int(time.time())}"
    signature = hmac.new(
        settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_session_cookie(cookie: str | None) -> bool:
    if not cookie:
        return False

    settings = get_settings()
    try:
        payload, signature = cookie.rsplit(".", 1)
        marker, issued_at_raw = payload.split(":", 1)
        issued_at = int(issued_at_raw)
    except (ValueError, AttributeError):
        return False

    if marker != "authenticated":
        return False

    expected_signature = hmac.new(
        settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return False

    if time.time() - issued_at > SESSION_MAX_AGE_SECONDS:
        return False

    return True


def require_auth(connection: HTTPConnection) -> None:
    settings = get_settings()
    if not settings.auth_username or not settings.auth_password:
        return

    cookie = connection.cookies.get(SESSION_COOKIE_NAME)
    if verify_session_cookie(cookie):
        return

    next_path = connection.url.path
    # A 303 response with a Location header is followed natively by browsers
    # on normal top-level navigations (the only kind this server-rendered
    # app performs), so raising it from a dependency is enough to redirect
    # to the login page without any custom exception handler.
    raise HTTPException(
        status_code=303,
        headers={"Location": f"/login?next={next_path}"},
    )
