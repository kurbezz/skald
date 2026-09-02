import asyncio
from html import unescape
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.testclient import WebSocketDenialResponse

from skald.indexer.base import ReleaseResult
from skald.main import create_app
from skald.models import JobStatus, MediaJob, MediaType
from skald.routes import jobs as jobs_routes


class FakeIndexer:
    async def search(self, query: str) -> list[ReleaseResult]:
        return [
            ReleaseResult(
                title="The.Matrix.1999.1080p.BluRay.x264-GROUP",
                indexer="fake",
                size_bytes=1_000_000_000,
                seeders=10,
                leechers=1,
                download_url="magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
            )
        ]


class FakeQbit:
    def add_torrent(self, download_url, category):
        return "fakehash"

    def delete_torrent(self, torrent_hash, delete_files=True):
        return None

    def get_status(self, torrent_hash):
        raise NotImplementedError


class FailingQbit:
    def add_torrent(self, download_url, category):
        raise RuntimeError("401 Client Error: Unauthorized")

    def delete_torrent(self, torrent_hash, delete_files=True):
        raise RuntimeError("401 Client Error: Unauthorized")

    def get_status(self, torrent_hash):
        raise NotImplementedError


def test_search_grab_and_jobs_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        app.state.qbit = FakeQbit()

        search_response = client.get("/search", params={"q": "matrix", "type": "movie"})
        assert search_response.status_code == 200
        assert "The.Matrix.1999" in search_response.text

        grab_response = client.post(
            "/grab",
            data={
                "release_title": "The.Matrix.1999.1080p.BluRay.x264-GROUP",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "movie",
                "title": "The Matrix",
                "year": 1999,
            },
        )
        assert grab_response.status_code == 200

        jobs_response = client.get("/jobs")
        assert jobs_response.status_code == 200
        assert "The Matrix" in jobs_response.text

        active_response = client.get("/jobs?tab=active")
        assert active_response.status_code == 200
        assert "The Matrix" in active_response.text
        assert 'data-active-jobs' in active_response.text
        assert 'data-active-job-list' in active_response.text
        assert 'data-active-job-template' in active_response.text
        assert 'data-active-count' in active_response.text
        assert 'data-completed-count' in active_response.text
        assert 'active_jobs.js' in active_response.text

        completed_response = client.get("/jobs?tab=completed")
        assert completed_response.status_code == 200
        assert "The Matrix" not in completed_response.text
        assert 'data-active-jobs' not in completed_response.text
        assert 'active_jobs.js' not in completed_response.text


def test_root_redirects_to_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_root.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert response.headers["location"] == "/jobs"


def test_routes_require_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    app = create_app()

    with TestClient(app) as client:
        # No session cookie -> redirected to /login.
        no_cookie_response = client.get("/jobs", follow_redirects=False)
        assert no_cookie_response.status_code == 303
        assert no_cookie_response.headers["location"].startswith("/login")

        # Following redirects with no cookie lands on the login page HTML.
        followed = client.get("/jobs", follow_redirects=True)
        assert followed.status_code == 200
        assert "Log in" in followed.text

        # Wrong credentials -> redirected back to /login with an error, no cookie set.
        wrong_login = client.post(
            "/login",
            data={"username": "wronguser", "password": "wrongpass", "next": "/jobs"},
            follow_redirects=False,
        )
        assert wrong_login.status_code == 303
        assert wrong_login.headers["location"].startswith("/login?error=1")
        assert "session" not in wrong_login.cookies

        # Correct credentials -> session cookie set and redirected to `next`.
        login_response = client.post(
            "/login",
            data={"username": "testuser", "password": "testpass", "next": "/jobs"},
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        assert login_response.headers["location"] == "/jobs"
        assert "session" in login_response.cookies

        # Subsequent requests carry the session cookie automatically via the client jar.
        authenticated_response = client.get("/jobs")
        assert authenticated_response.status_code == 200

        # Logout clears the cookie and further requests are redirected again.
        client.get("/logout")
        assert client.get("/jobs", follow_redirects=False).status_code == 303


def test_job_websocket_requires_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ws-auth.db"))
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    app = create_app()

    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse) as exc_info:
            with client.websocket_connect("/ws/jobs/1"):
                pass

    assert exc_info.value.status_code == 303


def test_job_detail_websocket_streams_status(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ws.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.qbit = FakeQbit()
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.MOVIE,
                title="WS Movie",
                year=2020,
                release_title="WS.Movie.2020",
                qbit_hash="fakehash",
                category="skald-movie",
                status=JobStatus.DOWNLOADING,
                progress=0.25,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        with client.websocket_connect(f"/ws/jobs/{job_id}") as websocket:
            data = websocket.receive_json()
            assert data["status"] == "downloading"
            assert data["progress"] == 0.25

        detail_response = client.get(f"/jobs/{job_id}")
        assert detail_response.status_code == 200
        assert f'data-job-id="{job_id}"' in detail_response.text
        assert 'data-job-status="downloading"' in detail_response.text
        assert "job_status.js" in detail_response.text


def test_job_detail_without_year_shows_only_em_dash_in_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "no-year.db"))
    app = create_app()

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.MOVIE,
                title="Untitled Date",
                release_title="Untitled.Date",
                qbit_hash="fakehash",
                category="skald-movie",
            )
            session.add(job)
            session.commit()
            session.refresh(job)

        detail_response = client.get(f"/jobs/{job.id}")

    assert detail_response.status_code == 200
    assert '<div class="k">Year</div>' not in detail_response.text
    assert "—" in unescape(detail_response.text)


def test_active_jobs_websocket_streams_changed_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "active-ws.db"))
    app = create_app()

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.MOVIE,
                title="Active WS Movie",
                year=2020,
                release_title="Active.WS.Movie.2020",
                qbit_hash="fakehash",
                category="skald-movie",
                status=JobStatus.DOWNLOADING,
                progress=0.25,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        with client.websocket_connect("/ws/jobs/active") as websocket:
            initial = websocket.receive_json()
            assert initial == {
                "jobs": [
                    {
                        "id": job_id,
                        "type": "movie",
                        "title": "Active WS Movie",
                        "status": "downloading",
                        "progress": 0.25,
                    }
                ],
                "completed_count": 0,
            }

            with Session(app.state.engine) as session:
                changed = session.get(MediaJob, job_id)
                changed.status = JobStatus.ORGANIZED
                session.add(changed)
                session.commit()

            assert websocket.receive_json() == {"jobs": [], "completed_count": 1}


async def test_active_jobs_websocket_stops_polling_after_disconnect(monkeypatch):
    class DisconnectingWebSocket:
        def __init__(self):
            self.app = SimpleNamespace(state=SimpleNamespace(engine=object()))
            self.disconnect = asyncio.Event()
            self.receive_started = asyncio.Event()
            self.payloads = []

        async def accept(self):
            pass

        async def send_json(self, payload):
            self.payloads.append(payload)
            self.disconnect.set()

        async def receive(self):
            self.receive_started.set()
            await self.disconnect.wait()
            return {"type": "websocket.disconnect"}

    monkeypatch.setattr(
        jobs_routes,
        "active_jobs_payload",
        lambda engine: {"jobs": [], "completed_count": 0},
    )
    websocket = DisconnectingWebSocket()

    await asyncio.wait_for(jobs_routes.active_jobs_ws(websocket), timeout=0.1)

    assert websocket.receive_started.is_set()
    assert websocket.payloads == [{"jobs": [], "completed_count": 0}]


def test_grab_surfaces_qbittorrent_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test2.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.qbit = FailingQbit()

        response = client.post(
            "/grab",
            data={
                "release_title": "The.Matrix.1999.1080p.BluRay.x264-GROUP",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "movie",
                "title": "The Matrix",
                "year": 1999,
            },
        )

        assert response.status_code == 502
        assert "Failed to add torrent" in response.text

        jobs_response = client.get("/jobs")
        assert "The Matrix" not in jobs_response.text


def test_delete_job_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test3.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.qbit = FakeQbit()
        client.post(
            "/grab",
            data={
                "release_title": "The.Matrix.1999.1080p.BluRay.x264-GROUP",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "movie",
                "title": "The Matrix",
                "year": 1999,
            },
        )

        response = client.post("/jobs/1/delete", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/jobs"
        jobs_response = client.get("/jobs")
        assert "The Matrix" not in jobs_response.text


def test_delete_job_removes_organized_library_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test-organized.db"))
    app = create_app()
    library_file = tmp_path / "library" / "Movie (2020)" / "Movie (2020).mkv"
    library_file.parent.mkdir(parents=True)
    library_file.write_text("data")

    with TestClient(app) as client:
        app.state.qbit = FakeQbit()
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.MOVIE,
                title="Movie",
                year=2020,
                release_title="Movie.2020",
                qbit_hash="fakehash",
                category="skald-movie",
                status=JobStatus.ORGANIZED,
                library_path=str(library_file),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        response = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

        assert response.status_code == 303
        assert not library_file.exists()
        assert not library_file.parent.exists()
        jobs_response = client.get("/jobs")
        assert "Movie" not in jobs_response.text


def test_delete_job_surfaces_qbittorrent_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test4.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.qbit = FakeQbit()
        client.post(
            "/grab",
            data={
                "release_title": "The.Matrix.1999.1080p.BluRay.x264-GROUP",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "movie",
                "title": "The Matrix",
                "year": 1999,
            },
        )
        app.state.qbit = FailingQbit()

        response = client.post("/jobs/1/delete")

        assert response.status_code == 502
        assert "Failed to delete torrent" in response.text
        jobs_response = client.get("/jobs")
        assert "The Matrix" in jobs_response.text
