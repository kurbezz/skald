from fastapi.testclient import TestClient

from skald.auth import create_csrf_token
from skald.flash import FLASH_COOKIE_NAME
from skald.main import create_app
from tests.test_quality_routes import _quality_form


def _save_quality(client):
    return client.post(
        "/quality/movie",
        data=_quality_form(),
        follow_redirects=False,
    )


def test_flash_survives_redirect_and_shows_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "flash.db"))
    app = create_app()
    with TestClient(app) as client:
        response = _save_quality(client)
        assert response.status_code == 303
        assert FLASH_COOKIE_NAME in response.headers["set-cookie"]
        # Following the redirect renders the message...
        first = client.get(response.headers["location"])
        assert "Movie profile saved." in first.text
        assert 'class="alert alert-success flash"' in first.text
        # ...and the one-shot cookie is gone on the next page.
        assert FLASH_COOKIE_NAME not in client.cookies
        second = client.get("/quality?profile=movie")
        assert "Movie profile saved." not in second.text


def test_redirect_response_does_not_consume_flash(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "flash-redirect.db"))
    app = create_app()
    with TestClient(app) as client:
        _save_quality(client)
        redirect = client.get("/", follow_redirects=False)
        assert redirect.status_code in (302, 307)
        assert FLASH_COOKIE_NAME in client.cookies


def test_tampered_flash_cookie_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "flash-tamper.db"))
    app = create_app()
    with TestClient(app) as client:
        response = _save_quality(client)
        value = client.cookies.get(FLASH_COOKIE_NAME)
        assert value
        payload, signature = value.rsplit(".", 1)
        client.cookies.clear()
        client.cookies.set(FLASH_COOKIE_NAME, f"{payload}.{'0' * len(signature)}")
        assert "Movie profile saved." not in client.get("/quality?profile=movie").text
        client.cookies.clear()
        client.cookies.set(FLASH_COOKIE_NAME, "garbage")
        assert client.get("/quality?profile=movie").status_code == 200
