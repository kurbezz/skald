import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from skald.auth import require_auth


def test_require_auth_disabled_when_not_configured(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "")
    monkeypatch.setenv("AUTH_PASSWORD", "")

    require_auth(credentials=None)


def test_require_auth_rejects_missing_credentials_when_configured(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")

    with pytest.raises(HTTPException) as exc_info:
        require_auth(credentials=None)

    assert exc_info.value.status_code == 401


def test_require_auth_rejects_wrong_credentials_when_configured(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")

    with pytest.raises(HTTPException) as exc_info:
        require_auth(credentials=HTTPBasicCredentials(username="wronguser", password="wrongpass"))

    assert exc_info.value.status_code == 401


def test_require_auth_accepts_correct_credentials_when_configured(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")

    require_auth(credentials=HTTPBasicCredentials(username="testuser", password="testpass"))
