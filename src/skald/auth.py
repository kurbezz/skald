import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlencode, urlsplit

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


async def require_csrf(request: Request) -> None:
    """Route dependency rejecting state-changing form posts without a valid token."""
    if not await verify_csrf_form(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


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


DEFAULT_NEXT = "/jobs"


def _quote_keep_slash(value, safe="", encoding=None, errors=None):
    return quote(value, safe="/", encoding=encoding, errors=errors)


def safe_next(value: str | None) -> str:
    """Return *value* if it is a safe internal path, else the default."""
    if not value or not isinstance(value, str):
        return DEFAULT_NEXT
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return DEFAULT_NEXT
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return DEFAULT_NEXT
    if "\\" in value:
        return DEFAULT_NEXT
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return DEFAULT_NEXT
    return value


def _referer_next(connection: HTTPConnection) -> str:
    referer = connection.headers.get("referer")
    if not referer:
        return DEFAULT_NEXT
    parts = urlsplit(referer)
    if parts.netloc and parts.netloc != connection.headers.get("host", connection.url.netloc):
        return DEFAULT_NEXT
    target = parts.path
    if parts.query:
        target += f"?{parts.query}"
    return safe_next(target)


def require_auth(connection: HTTPConnection) -> None:
    settings = get_settings()
    if not settings.auth_username or not settings.auth_password:
        return

    cookie = connection.cookies.get(SESSION_COOKIE_NAME)
    if verify_session_cookie(cookie):
        return

    if connection.scope["type"] == "websocket":
        login_params = {"next": safe_next(connection.url.path)}
    else:
        method = connection.scope.get("method", "GET").upper()
        if method in ("GET", "HEAD"):
            target = connection.url.path
            if connection.url.query:
                target += f"?{connection.url.query}"
            login_params = {"next": safe_next(target)}
        else:
            login_params = {
                "expired": 1,
                "next": _referer_next(connection),
            }
    # A 303 response with a Location header is followed natively by browsers
    # on normal top-level navigations (the only kind this server-rendered
    # app performs), so raising it from a dependency is enough to redirect
    # to the login page without any custom exception handler.
    raise HTTPException(
        status_code=303,
        headers={"Location": f"/login?{urlencode(login_params, quote_via=_quote_keep_slash)}"},
    )


def safe_back_url(connection: HTTPConnection) -> str:
    """Same-host Referer path (query kept) or the default; used by error pages."""
    return _referer_next(connection)
