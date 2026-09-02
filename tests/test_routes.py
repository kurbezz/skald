from fastapi.testclient import TestClient

from skald.indexer.base import ReleaseResult
from skald.main import create_app


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
