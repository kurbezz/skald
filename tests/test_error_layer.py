import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from skald.auth import create_csrf_token
from skald.main import create_app

HTML = {"accept": "text/html"}


class FailingIndexer:
    base_url = "http://jackett:9117"

    def __init__(self, exc):
        self.exc = exc

    async def search(self, query):
        raise self.exc


def _status_error(code):
    url = "http://jackett:9117/api?apikey=SECRETKEY&q=x"
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"Client error '{code}' for url '{url}'", request=request, response=httpx.Response(code, request=request)
    )


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_status_error(401), "HTTP 401"),
        (httpx.ReadTimeout("timeout apikey=SECRETKEY"), "timed out"),
        (httpx.ConnectError("refused apikey=SECRETKEY"), "http://jackett:9117"),
    ],
)
def test_search_indexer_errors_do_not_leak_api_key(tmp_path, monkeypatch, exc, expected):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    with TestClient(app) as client:
        app.state.indexer = FailingIndexer(exc)
        response = client.get("/search?q=x")
    assert response.status_code == 200
    assert expected in response.text
    assert "apikey" not in response.text
    assert "SECRETKEY" not in response.text


def test_search_unknown_type_falls_back_to_movie(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/search?type=foo")
    assert response.status_code == 200


def test_grab_unknown_type_is_422_html(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/grab",
            data={
                "csrf_token": create_csrf_token(None),
                "release_title": "X",
                "download_url": "magnet:?xt=urn:btih:" + "a" * 40,
                "media_type": "foo",
                "title": "X",
            },
            headers=HTML,
        )
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]


def test_missing_job_is_404_html(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/jobs/999", headers=HTML)
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


def test_validation_error_html_lists_fields_and_json_stays_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    with TestClient(app) as client:
        html = client.get("/jobs/abc", headers=HTML)
        js = client.get("/jobs/abc", headers={"accept": "application/json"})
    assert html.status_code == 422
    assert "Some fields are invalid" in html.text
    assert "job_id" in html.text
    assert js.status_code == 422
    assert "detail" in js.json()


def test_html_403_has_human_title_and_safe_back_link(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/jobs/1/delete",
            headers={**HTML, "referer": "http://testserver/jobs?tab=completed"},
        )
        evil = client.post(
            "/jobs/1/delete", headers={**HTML, "referer": "http://evil.example/x"}
        )
    assert response.status_code == 403
    assert "Session check failed" in response.text
    assert "/jobs?tab=completed" in response.text
    assert "evil.example" not in evil.text


def test_auth_redirect_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/jobs", headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_sec_fetch_site_cross_site_post_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    data = {"csrf_token": create_csrf_token(None)}
    with TestClient(app) as client:
        cross = client.post("/jobs/999/delete", data=data, headers={**HTML, "sec-fetch-site": "cross-site"})
        same = client.post(
            "/jobs/999/delete", data=data, headers={"sec-fetch-site": "same-origin"}, follow_redirects=False
        )
        missing = client.post("/jobs/999/delete", data=data, follow_redirects=False)
        get = client.get("/jobs", headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 403
    assert "text/html" in cross.headers["content-type"]
    assert same.status_code == 303
    assert missing.status_code == 303
    assert get.status_code == 200


def _iter_routes(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _iter_routes(inner.routes)
        else:
            yield route


def _post_paths(app):
    return sorted(
        {
            route.path
            for route in _iter_routes(app.routes)
            if isinstance(route, APIRoute) and "POST" in route.methods and route.path != "/login"
        }
    )


def test_every_mutating_route_requires_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    app = create_app()
    paths = _post_paths(app)
    assert paths
    with TestClient(app) as client:
        for path in paths:
            concrete = path
            for param in ("{job_id}", "{event_id}", "{subscription_id}", "{media_type}", "{release_id}"):
                concrete = concrete.replace(param, "1" if "type" not in param else "movie")
            assert "{" not in concrete, path
            response = client.post(concrete, data={}, follow_redirects=False)
            assert response.status_code == 403, path
