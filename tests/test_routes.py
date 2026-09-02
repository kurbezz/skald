from fastapi.testclient import TestClient
from sqlmodel import Session

from skald.indexer.base import ReleaseResult
from skald.main import create_app
from skald.models import JobStatus, MediaJob, MediaType


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

        completed_response = client.get("/jobs?tab=completed")
        assert completed_response.status_code == 200
        assert "The Matrix" not in completed_response.text


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
        assert client.get("/jobs").status_code == 401
        assert client.get("/jobs", auth=("wronguser", "wrongpass")).status_code == 401
        assert client.get("/jobs", auth=("testuser", "testpass")).status_code == 200


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
