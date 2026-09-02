import time

import pytest
from fastapi import HTTPException
from starlette.requests import HTTPConnection

from skald.auth import create_session_cookie, require_auth, verify_session_cookie
from skald.config import get_settings


def _fake_connection(cookie_value: str | None, path: str = "/jobs") -> HTTPConnection:
    cookie_header = f"session={cookie_value}".encode() if cookie_value is not None else b""
    scope = {
        "type": "http",
        "path": path,
        "headers": [(b"cookie", cookie_header)] if cookie_value is not None else [],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return HTTPConnection(scope)


def test_require_auth_disabled_when_not_configured(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "")
    monkeypatch.setenv("AUTH_PASSWORD", "")

    require_auth(_fake_connection(None))


def test_require_auth_redirects_when_no_cookie(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")

    with pytest.raises(HTTPException) as exc_info:
        require_auth(_fake_connection(None))

    assert exc_info.value.status_code == 303
    assert exc_info.value.headers["Location"].startswith("/login")


def test_require_auth_redirects_when_cookie_invalid(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")

    with pytest.raises(HTTPException) as exc_info:
        require_auth(_fake_connection("garbage"))

    assert exc_info.value.status_code == 303


def test_require_auth_passes_with_valid_cookie(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")

    cookie = create_session_cookie()
    require_auth(_fake_connection(cookie))


def test_create_and_verify_session_cookie_round_trip(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "another-secret")

    cookie = create_session_cookie()
    assert verify_session_cookie(cookie) is True


def test_verify_session_cookie_rejects_none():
    assert verify_session_cookie(None) is False


def test_verify_session_cookie_rejects_malformed():
    assert verify_session_cookie("not-a-valid-cookie") is False
    assert verify_session_cookie("authenticated:notanumber.deadbeef") is False


def test_verify_session_cookie_rejects_tampered_signature(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "yet-another-secret")

    cookie = create_session_cookie()
    payload, signature = cookie.rsplit(".", 1)
    flipped_char = "0" if signature[0] != "0" else "1"
    tampered = f"{payload}.{flipped_char}{signature[1:]}"
    assert verify_session_cookie(tampered) is False


def test_verify_session_cookie_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "secret-a")
    cookie = create_session_cookie()

    monkeypatch.setenv("SECRET_KEY", "secret-b")
    assert verify_session_cookie(cookie) is False


def test_verify_session_cookie_rejects_expired(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "expiry-secret")
    settings = get_settings()

    import hashlib
    import hmac

    issued_at = int(time.time()) - (31 * 24 * 3600)
    payload = f"authenticated:{issued_at}"
    signature = hmac.new(
        settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    expired_cookie = f"{payload}.{signature}"

    assert verify_session_cookie(expired_cookie) is False
