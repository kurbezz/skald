import asyncio
import re
from html import unescape
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select
from starlette.testclient import WebSocketDenialResponse

from skald.indexer.base import ReleaseResult
from skald.lifecycle import file_identity
from skald.main import create_app
from skald.models import FileLifecycle, JobStatus, MediaJob, MediaType, OrganizationMode, OrganizedFile
from skald.routes import jobs as jobs_routes
from skald.routes import search as search_routes


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


class MultiResultIndexer:
    async def search(self, query: str) -> list[ReleaseResult]:
        return [
            ReleaseResult(
                title="Seeder.High.2024.1080p",
                indexer="fake",
                size_bytes=2_000_000_000,
                seeders=20,
                leechers=2,
                download_url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
            ),
            ReleaseResult(
                title="Leecher.High.2024.1080p",
                indexer="fake",
                size_bytes=3_000_000_000,
                seeders=5,
                leechers=30,
                download_url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222",
            ),
            ReleaseResult(
                title="Middle.Peers.2024.1080p",
                indexer="fake",
                size_bytes=1_000_000_000,
                seeders=10,
                leechers=10,
                download_url="magnet:?xt=urn:btih:3333333333333333333333333333333333333333",
            ),
        ]


class FakeQbit:
    def add_torrent(self, download_url, category):
        return "fakehash"

    def delete_torrent(self, torrent_hash, delete_files=True):
        return None

    def get_status(self, torrent_hash):
        raise NotImplementedError


class RecordingQbit:
    def __init__(self):
        self.add_calls = []

    def add_torrent(self, download_url, category):
        self.add_calls.append((download_url, category))
        return "fakehash"


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


def test_search_hides_grab_metadata_when_movie_parse_is_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "complete_movie.db"))
    monkeypatch.setattr(
        search_routes,
        "parse_release",
        lambda _: {"title": "The Matrix", "year": 1999, "season": None, "episode": None},
    )
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie"})

    assert response.status_code == 200
    assert 'name="title" value="The Matrix"' in response.text
    assert 'name="year" value="1999"' in response.text
    assert "data-grab-review-toggle" not in response.text
    assert 'type="text" name="title"' not in response.text


def test_search_grabs_complete_tv_parse_without_year(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "complete_tv_without_year.db"))
    monkeypatch.setattr(
        search_routes,
        "parse_release",
        lambda _: {"title": "Breaking Bad", "year": None, "season": 1, "episode": 5},
    )
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        app.state.qbit = FakeQbit()
        response = client.get("/search", params={"q": "breaking bad", "type": "tv"})

        assert response.status_code == 200
        assert "data-grab-review-toggle" not in response.text
        assert 'name="title" value="Breaking Bad"' in response.text
        assert 'name="season" value="1"' in response.text
        assert 'name="episode" value="5"' in response.text
        assert 'value="None"' not in response.text

        grab_response = client.post(
            "/grab",
            data={
                "release_title": "Breaking.Bad.S01E05.720p.HDTV.x264-GROUP",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "tv",
                "title": "Breaking Bad",
                "season": 1,
                "episode": 5,
            },
            follow_redirects=False,
        )

    assert grab_response.status_code == 303
    with Session(app.state.engine) as session:
        job = session.exec(select(MediaJob)).one()
    assert job.type == MediaType.TV
    assert job.title == "Breaking Bad"
    assert job.year is None
    assert job.season == 1
    assert job.episode == 5


def test_grab_persists_normalized_tv_episode_set_before_adding_torrent(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "grab-tv-pack.db"))
    app = create_app()

    with TestClient(app) as client:
        qbit = RecordingQbit()
        app.state.qbit = qbit
        response = client.post(
            "/grab",
            data={
                "release_title": "Show.S01E01-E06.1080p",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "tv",
                "title": "Show",
                "season": 1,
                "episode": 1,
                "episode_set": "1-6",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert len(qbit.add_calls) == 1
    with Session(app.state.engine) as session:
        job = session.exec(select(MediaJob)).one()
    assert job.episode == 1
    assert job.episode_set == "[1,2,3,4,5,6]"


@pytest.mark.parametrize(
    "episode_set",
    [
        pytest.param("1-2-3", id="malformed"),
        pytest.param("6-1", id="reversed"),
        pytest.param("0", id="zero"),
        pytest.param("2-6", id="start-mismatch"),
    ],
)
def test_grab_rejects_invalid_tv_episode_sets_before_adding_torrent(
    tmp_path, monkeypatch, episode_set
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "invalid-tv-episode-set.db"))
    app = create_app()

    with TestClient(app) as client:
        qbit = RecordingQbit()
        app.state.qbit = qbit
        response = client.post(
            "/grab",
            data={
                "release_title": "Show.S01E01-E06.1080p",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "tv",
                "title": "Show",
                "season": 1,
                "episode": 1,
                "episode_set": episode_set,
            },
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert qbit.add_calls == []


def test_grab_rejects_explicitly_empty_tv_episode_set_before_adding_torrent(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "empty-tv-episode-set.db"))
    app = create_app()

    with TestClient(app) as client:
        qbit = RecordingQbit()
        app.state.qbit = qbit
        response = client.post(
            "/grab",
            data={
                "release_title": "Show.S01E01-E06.1080p",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "tv",
                "title": "Show",
                "season": 1,
                "episode": 1,
                "episode_set": "",
            },
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert qbit.add_calls == []


def test_grab_rejects_episode_sets_supplied_for_movies(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "movie-episode-set.db"))
    app = create_app()

    with TestClient(app) as client:
        qbit = RecordingQbit()
        app.state.qbit = qbit
        response = client.post(
            "/grab",
            data={
                "release_title": "Movie.2024.1080p",
                "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
                "media_type": "movie",
                "title": "Movie",
                "year": 2024,
                "episode_set": "1-6",
            },
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert qbit.add_calls == []


def test_search_renders_complete_tv_multi_episode_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "complete_tv_multi_episode.db"))
    monkeypatch.setattr(
        search_routes,
        "parse_release",
        lambda _: {
            "title": "Black Mirror",
            "year": 2025,
            "season": 7,
            "episode": 1,
            "episode_set": (1, 2, 3, 4, 5, 6),
        },
    )
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        response = client.get("/search", params={"q": "black mirror", "type": "tv"})

    assert response.status_code == 200
    assert "data-grab-review-toggle" not in response.text
    assert 'name="episode" value="1"' in response.text
    assert re.search(
        r'<input\b[^>]*\btype="hidden"[^>]*\bname="episode_set"[^>]*'
        r'\bvalue="\[1,2,3,4,5,6\]"[^>]*>',
        response.text,
    )
    assert 'name="episode" value="[1, 2, 3, 4, 5, 6]"' not in response.text
    assert "E01-E06" in response.text


def test_search_renders_multi_episode_set_in_incomplete_review(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "incomplete_tv_multi_episode.db"))
    monkeypatch.setattr(
        search_routes,
        "parse_release",
        lambda _: {
            "title": None,
            "year": 2025,
            "season": 7,
            "episode": 1,
            "episode_set": (1, 2, 3, 4, 5, 6),
        },
    )
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        response = client.get("/search", params={"q": "black mirror", "type": "tv"})

    assert response.status_code == 200
    assert "data-grab-review-toggle" in response.text
    assert 'name="episode" value="1"' in response.text
    assert re.search(
        r'<input\b[^>]*\btype="text"[^>]*\bname="episode_set"[^>]*'
        r'\bvalue="1-6"[^>]*\brequired\b[^>]*>',
        response.text,
    )
    for name in ("title", "season", "episode"):
        assert re.search(
            rf'<input\b[^>]*\bname="{name}"[^>]*\brequired\b[^>]*>', response.text
        )


@pytest.mark.parametrize(
    ("media_type", "invalid_field", "invalid_value"),
    [
        pytest.param("movie", "title", None, id="movie-missing-title"),
        pytest.param("movie", "title", "", id="movie-blank-title"),
        pytest.param("movie", "year", None, id="movie-missing-year"),
        pytest.param("movie", "year", "", id="movie-blank-year"),
        pytest.param("tv", "title", None, id="tv-missing-title"),
        pytest.param("tv", "title", "", id="tv-blank-title"),
        pytest.param("tv", "season", None, id="tv-missing-season"),
        pytest.param("tv", "season", "", id="tv-blank-season"),
        pytest.param("tv", "episode", None, id="tv-missing-episode"),
        pytest.param("tv", "episode", "", id="tv-blank-episode"),
    ],
)
def test_grab_rejects_missing_or_blank_required_metadata(
    tmp_path, monkeypatch, media_type, invalid_field, invalid_value
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "invalid-grab-metadata.db"))
    app = create_app()
    metadata = {
        "release_title": "Example.Release",
        "download_url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD",
        "media_type": media_type,
        "title": "Example Title",
        "year": 2024,
        "season": 1,
        "episode": 5,
    }
    if invalid_value is None:
        del metadata[invalid_field]
    else:
        metadata[invalid_field] = invalid_value

    with TestClient(app) as client:
        qbit = RecordingQbit()
        app.state.qbit = qbit
        response = client.post("/grab", data=metadata, follow_redirects=False)

    assert response.status_code == 422
    assert qbit.add_calls == []


@pytest.mark.parametrize(
    ("media_type", "guess", "expected_names", "required_names", "optional_names"),
    [
        (
            "movie",
            {"title": "The Matrix", "year": None, "season": None, "episode": None},
            ["title", "year"],
            ["title", "year"],
            [],
        ),
        (
            "tv",
            {"title": "Breaking Bad", "year": None, "season": 1, "episode": None},
            ["title", "year", "season", "episode"],
            ["title", "season", "episode"],
            ["year"],
        ),
    ],
)
def test_search_marks_incomplete_metadata_for_review(
    tmp_path, monkeypatch, media_type, guess, expected_names, required_names, optional_names
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / f"incomplete_{media_type}.db"))
    monkeypatch.setattr(search_routes, "parse_release", lambda _: guess)
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        response = client.get("/search", params={"q": "matrix", "type": media_type})

    assert response.status_code == 200
    assert "Needs review" in response.text
    assert "data-grab-review-toggle" in response.text
    assert 'aria-expanded="false"' in response.text
    assert "data-grab-review-fields hidden" in response.text
    for name in expected_names:
        assert f'name="{name}"' in response.text
    for name in required_names:
        assert re.search(
            rf'<input\b[^>]*\bname="{name}"[^>]*\brequired\b[^>]*>', response.text
        )
    for name in optional_names:
        input_tag = re.search(rf'<input\b[^>]*\bname="{name}"[^>]*>', response.text)
        assert input_tag is not None
        assert "required" not in input_tag.group()


@pytest.mark.parametrize(
    ("params", "expected_titles"),
    [
        ({}, ["Seeder.High", "Middle.Peers", "Leecher.High"]),
        ({"sort": "seeders", "direction": "asc"}, ["Leecher.High", "Middle.Peers", "Seeder.High"]),
        ({"sort": "leechers", "direction": "desc"}, ["Leecher.High", "Middle.Peers", "Seeder.High"]),
        ({"sort": "leechers", "direction": "asc"}, ["Seeder.High", "Middle.Peers", "Leecher.High"]),
        (
            {"sort": "size_bytes", "direction": "desc"},
            ["Leecher.High", "Seeder.High", "Middle.Peers"],
        ),
        (
            {"sort": "size_bytes", "direction": "asc"},
            ["Middle.Peers", "Seeder.High", "Leecher.High"],
        ),
        ({"sort": "unknown", "direction": "up"}, ["Seeder.High", "Middle.Peers", "Leecher.High"]),
        pytest.param(
            {"sort": "invalid", "direction": "asc"},
            ["Seeder.High", "Middle.Peers", "Leecher.High"],
            id="invalid-sort-with-valid-direction-defaults",
        ),
        pytest.param(
            {"sort": "leechers", "direction": "invalid"},
            ["Seeder.High", "Middle.Peers", "Leecher.High"],
            id="valid-sort-with-invalid-direction-defaults",
        ),
    ],
)
def test_search_sorts_peers(tmp_path, monkeypatch, params, expected_titles):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search_sort.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = MultiResultIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie", **params})

    assert response.status_code == 200
    positions = [response.text.index(title) for title in expected_titles]
    assert positions == sorted(positions)


def test_search_renders_sort_links(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search_sort_links.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = MultiResultIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie"})

    assert response.status_code == 200
    assert 'href="/search?q=matrix&amp;type=movie&amp;sort=seeders&amp;direction=asc"' in response.text
    assert 'href="/search?q=matrix&amp;type=movie&amp;sort=leechers&amp;direction=desc"' in response.text
    assert 'href="/search?q=matrix&amp;type=movie&amp;sort=size_bytes&amp;direction=desc"' in response.text


@pytest.mark.parametrize(
    ("params", "expected_aria_sort"),
    [
        pytest.param({}, "descending", id="default-seeders-descending"),
        pytest.param(
            {"sort": "seeders", "direction": "asc"},
            "ascending",
            id="seeders-ascending",
        ),
    ],
)
def test_search_renders_active_sort_header_aria(tmp_path, monkeypatch, params, expected_aria_sort):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search_sort_header_aria.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = MultiResultIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie", **params})

    assert response.status_code == 200
    seeders_header = re.search(
        r"<th\b[^>]*>\s*<a\b[^>]*>[^<]*Seeders(?:[^<]|<(?!/th\b))*</th>",
        response.text,
        re.DOTALL,
    )
    leechers_header = re.search(
        r"<th\b[^>]*>\s*<a\b[^>]*>[^<]*Leechers(?:[^<]|<(?!/th\b))*</th>",
        response.text,
        re.DOTALL,
    )
    assert seeders_header is not None
    assert leechers_header is not None
    assert f'aria-sort="{expected_aria_sort}"' in seeders_header.group()
    assert "aria-sort" not in leechers_header.group()


@pytest.mark.parametrize(
    ("params", "expected_aria_sort", "expected_next_direction"),
    [
        pytest.param(
            {"sort": "size_bytes", "direction": "desc"},
            "descending",
            "asc",
            id="size-descending",
        ),
        pytest.param(
            {"sort": "size_bytes", "direction": "asc"},
            "ascending",
            "desc",
            id="size-ascending",
        ),
    ],
)
def test_search_renders_active_size_sort_header_aria(
    tmp_path, monkeypatch, params, expected_aria_sort, expected_next_direction
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search_size_header_aria.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = MultiResultIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie", **params})

    assert response.status_code == 200
    size_header = re.search(
        r"<th\b[^>]*>\s*<a\b[^>]*>[^<]*Size(?:[^<]|<(?!/th\b))*</th>",
        response.text,
        re.DOTALL,
    )
    seeders_header = re.search(
        r"<th\b[^>]*>\s*<a\b[^>]*>[^<]*Seeders(?:[^<]|<(?!/th\b))*</th>",
        response.text,
        re.DOTALL,
    )
    assert size_header is not None
    assert seeders_header is not None
    assert f'aria-sort="{expected_aria_sort}"' in size_header.group()
    assert "aria-sort" not in seeders_header.group()
    assert (
        f'href="/search?q=matrix&amp;type=movie&amp;sort=size_bytes&amp;direction={expected_next_direction}"'
        in size_header.group()
    )


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


@pytest.mark.parametrize(
    ("stored_episode_set", "expected_label", "expected_input"),
    [
        pytest.param("[6,5,4,3,2,1]", "E01-E06", "1-6", id="normalizes-storage"),
        pytest.param("not an episode set", "", "", id="ignores-invalid-storage"),
    ],
)
def test_job_detail_supplies_safe_episode_set_context(
    tmp_path, monkeypatch, stored_episode_set, expected_label, expected_input
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "detail-episode-set.db"))
    app = create_app()
    contexts = []

    def capture_template_response(request, name, context, status_code=200):
        contexts.append(context)
        return HTMLResponse("captured", status_code=status_code)

    monkeypatch.setattr(jobs_routes.templates, "TemplateResponse", capture_template_response)

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV,
                title="Show",
                season=1,
                episode=1,
                episode_set=stored_episode_set,
                release_title="Show.S01E01-E06",
                qbit_hash="hash",
                category="skald-tv",
            )
            session.add(job)
            session.commit()
            job_id = job.id

        response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert contexts[-1]["episode_label"] == expected_label
    assert contexts[-1]["episode_set_input"] == expected_input


def test_job_lists_render_tv_season_and_episode_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "job-list-tv-labels.db"))
    app = create_app()

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            active_job = MediaJob(
                type=MediaType.TV,
                title="Active Show",
                season=1,
                episode=1,
                episode_set="[1,2,3]",
                release_title="Active.Show.S01E01-E03",
                qbit_hash="active-hash",
                category="skald-tv",
                status=JobStatus.DOWNLOADING,
            )
            completed_job = MediaJob(
                type=MediaType.TV,
                title="Completed Show",
                season=2,
                episode=5,
                release_title="Completed.Show.S02E05",
                qbit_hash="completed-hash",
                category="skald-tv",
                status=JobStatus.ORGANIZED,
            )
            session.add(active_job)
            session.add(completed_job)
            session.commit()

        active_response = client.get("/jobs?tab=active")
        completed_response = client.get("/jobs?tab=completed")

    assert active_response.status_code == 200
    assert 'data-job-episode>S01E01-E03</div>' in active_response.text
    assert completed_response.status_code == 200
    assert 'class="cell-muted">S02E05</div>' in completed_response.text


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
                        "season": None,
                        "episode": None,
                        "episode_set": None,
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


def test_delete_job_removes_all_recorded_pack_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pack-delete.db"))
    app = create_app()
    season = tmp_path / "library" / "Show" / "Season 01"
    created = [season / "Show - S01E01.mkv", season / "Show - S01E02.mkv"]
    preserved = season / "Show - S01E03.mkv"
    season.mkdir(parents=True)
    for path in [*created, preserved]:
        path.write_text("data")

    with TestClient(app) as client:
        app.state.qbit = FakeQbit()
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV,
                title="Show",
                release_title="Show.S01",
                qbit_hash="hash",
                category="skald-tv",
                status=JobStatus.ORGANIZED,
                organization_mode=OrganizationMode.PACK,
                operation_token="organize-token",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id
            for path in created:
                identity = file_identity(path)
                session.add(OrganizedFile(
                    job_id=job.id, path=str(path), lifecycle=FileLifecycle.PUBLISHED,
                    operation_token="organize-token",
                    published_device=identity.device, published_inode=identity.inode,
                ))
            session.commit()

        response = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert not any(path.exists() for path in created)
    assert preserved.exists()


def test_scalar_delete_commits_deleting_before_library_side_effect(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "delete-intent.db"))
    app = create_app()
    library_file = tmp_path / "library" / "Movie.mkv"
    library_file.parent.mkdir(parents=True)
    library_file.write_text("data")
    # Scalar deletion's filesystem removal happens inside
    # worker.reconcile_deleting_job, not the route module itself.
    import skald.worker as worker_module
    original_remove = worker_module.remove_organized_file
    qbit_calls = []

    class RecordingQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            qbit_calls.append(torrent_hash)

    with TestClient(app) as client:
        app.state.qbit = RecordingQbit()
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.MOVIE, title="Movie", year=2020,
                release_title="Movie.2020", qbit_hash="hash", category="skald-movie",
                status=JobStatus.ORGANIZED, library_path=str(library_file),
            )
            session.add(job)
            session.commit()
            job_id = job.id

        def assert_deletion_intent(path):
            with Session(app.state.engine) as verifier:
                assert verifier.get(MediaJob, job_id).status == JobStatus.DELETING
            original_remove(path)

        monkeypatch.setattr("skald.worker.remove_organized_file", assert_deletion_intent)
        response = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert qbit_calls == ["hash"]


def test_delete_job_retains_deleting_ledger_when_second_library_file_fails(tmp_path, monkeypatch):
    """A pack row's owned-file cleanup uses identity-checked
    `cleanup_owned_file` (not path-only removal); a cleanup error on one row
    must retain `DELETING` and the entire ledger for retry, and must never
    reach qBittorrent.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "delete-library-failure.db"))
    app = create_app()
    paths = [tmp_path / "library" / f"Show - S01E{episode:02d}.mkv" for episode in range(1, 3)]
    paths[0].parent.mkdir(parents=True)
    for path in paths:
        path.write_text("data")
    import skald.worker as worker_module
    original_cleanup = worker_module.cleanup_owned_file
    qbit_calls = []

    class RecordingQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            qbit_calls.append(torrent_hash)

    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.qbit = RecordingQbit()
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV, title="Show", release_title="Show.S01",
                qbit_hash="hash", category="skald-tv", status=JobStatus.ORGANIZED,
                organization_mode=OrganizationMode.PACK, operation_token="organize-token",
            )
            session.add(job)
            session.commit()
            for path in paths:
                identity = file_identity(path)
                session.add(OrganizedFile(
                    job_id=job.id, path=str(path), lifecycle=FileLifecycle.PUBLISHED,
                    operation_token="organize-token",
                    published_device=identity.device, published_inode=identity.inode,
                ))
            session.commit()
            job_id = job.id

        def fail_second_cleanup(path, identity):
            if path == paths[1]:
                from skald.organizer import CleanupOutcome
                return CleanupOutcome(removed=False, foreign=False, error="permission denied")
            return original_cleanup(path, identity)

        monkeypatch.setattr("skald.worker.cleanup_owned_file", fail_second_cleanup)
        response = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

    assert response.status_code == 500
    assert qbit_calls == []
    with Session(app.state.engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING
        assert "Delete pending:" in job.error_message
        assert {row.path for row in session.exec(select(OrganizedFile)).all()} == {str(path) for path in paths}


def test_delete_job_retains_deleting_ledger_when_qbittorrent_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "delete-qbit-failure.db"))
    app = create_app()
    library_file = tmp_path / "library" / "Show - S01E01.mkv"
    library_file.parent.mkdir(parents=True)
    library_file.write_text("data")

    with TestClient(app) as client:
        app.state.qbit = FailingQbit()
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV, title="Show", release_title="Show.S01",
                qbit_hash="hash", category="skald-tv", status=JobStatus.ORGANIZED,
                organization_mode=OrganizationMode.PACK, operation_token="organize-token",
            )
            session.add(job)
            session.commit()
            identity = file_identity(library_file)
            session.add(OrganizedFile(
                job_id=job.id, path=str(library_file), lifecycle=FileLifecycle.PUBLISHED,
                operation_token="organize-token",
                published_device=identity.device, published_inode=identity.inode,
            ))
            session.commit()
            job_id = job.id

        response = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

    assert response.status_code == 502
    assert not library_file.exists()
    with Session(app.state.engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING
        assert "Delete pending:" in job.error_message
        assert [row.path for row in session.exec(select(OrganizedFile)).all()] == [str(library_file)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("title", None, id="missing-title"),
        pytest.param("title", "", id="blank-title"),
        pytest.param("season", None, id="missing-season"),
        pytest.param("season", "", id="blank-season"),
        pytest.param("episode", None, id="missing-episode"),
        pytest.param("episode", "", id="blank-episode"),
        pytest.param("episode_set", "6-1", id="invalid-episode-set"),
        pytest.param("episode_set", "", id="empty-episode-set"),
    ],
)
def test_retry_rejects_invalid_tv_metadata_without_mutating_job(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "invalid-retry-tv-metadata.db"))
    app = create_app()

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV,
                title="Original Show",
                season=1,
                episode=1,
                episode_set="[1,2,3,4,5,6]",
                release_title="Show.S01E01-E06",
                qbit_hash="hash",
                category="skald-tv",
                status=JobStatus.NEEDS_ATTENTION,
                error_message="original error",
            )
            session.add(job)
            session.commit()
            job_id = job.id

        data = {"title": "Updated Show", "season": 1, "episode": 1, "episode_set": "1-6"}
        if value is None:
            del data[field]
        else:
            data[field] = value
        response = client.post(f"/jobs/{job_id}/retry", data=data, follow_redirects=False)

    assert response.status_code == 422
    with Session(app.state.engine) as session:
        unchanged = session.get(MediaJob, job_id)
    assert unchanged.status == JobStatus.NEEDS_ATTENTION
    assert unchanged.title == "Original Show"
    assert unchanged.season == 1
    assert unchanged.episode == 1
    assert unchanged.episode_set == "[1,2,3,4,5,6]"
    assert unchanged.error_message == "original error"


def test_retry_tv_pack_with_residual_ledger_commits_organizing_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "retry-pack.db"))
    app = create_app()
    residual = tmp_path / "library" / "Show" / "Season 01" / "Show - S01E01.mkv"
    residual.parent.mkdir(parents=True)
    residual.write_text("partial")

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV, title="Show", release_title="Show.S01",
                qbit_hash="hash", category="skald-tv", status=JobStatus.NEEDS_ATTENTION,
                season=1, episode=1,
            )
            session.add(job)
            session.commit()
            session.add(OrganizedFile(
                job_id=job.id, path=str(residual), lifecycle=FileLifecycle.LEGACY_UNVERIFIED
            ))
            session.commit()
            job_id = job.id

        response = client.post(
            f"/jobs/{job_id}/retry",
            data={
                "title": "Show",
                "season": 1,
                "episode": 1,
                "episode_set": "[6,5,4,3,2,1]",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        with Session(app.state.engine) as session:
            retried = session.get(MediaJob, job_id)
            assert retried.status == JobStatus.ORGANIZING
            assert retried.episode == 1
            assert retried.episode_set == "[1,2,3,4,5,6]"


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


def test_fenced_delete_route_does_nothing_while_job_lock_is_held_elsewhere(tmp_path, monkeypatch):
    """`/jobs/{id}/delete` must acquire the shared per-job lock before any
    durable transition or side effect; a caller that cannot acquire the
    lock performs no work and leaves the job for the lock holder to finish.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "delete-lock-contention.db"))
    app = create_app()

    class RecordingQbit:
        def __init__(self):
            self.deleted_hashes = []

        def delete_torrent(self, torrent_hash, delete_files=True):
            self.deleted_hashes.append(torrent_hash)

    with TestClient(app) as client:
        fake_qbit = RecordingQbit()
        app.state.qbit = fake_qbit
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV, title="Show", season=1, episode=1,
                release_title="Show.S01", qbit_hash="hash", category="skald-tv",
                status=JobStatus.ORGANIZED,
                organization_mode=OrganizationMode.PACK,
                operation_token="organize-token",
            )
            session.add(job)
            session.commit()
            ledger = OrganizedFile(
                job_id=job.id,
                path="/library/Show - S01E01.mkv",
                lifecycle=FileLifecycle.PUBLISHED,
                operation_token="organize-token",
                published_device=1,
                published_inode=1,
            )
            session.add(ledger)
            session.commit()
            job_id = job.id
            ledger_id = ledger.id

        from skald.lifecycle import try_job_lock

        with try_job_lock(job_id) as held:
            assert held
            response = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

        assert response.status_code == 409
        assert "already being updated" in response.text
        assert fake_qbit.deleted_hashes == []
        with Session(app.state.engine) as session:
            job = session.get(MediaJob, job_id)
            assert job.status == JobStatus.ORGANIZED
            assert job.operation_token == "organize-token"
            assert session.get(OrganizedFile, ledger_id).lifecycle == FileLifecycle.PUBLISHED


def test_fenced_retry_route_does_nothing_while_job_lock_is_held_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "retry-lock-contention.db"))
    app = create_app()

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV, title="Show", season=1, episode=1,
                release_title="Show.S01", qbit_hash="hash", category="skald-tv",
                status=JobStatus.NEEDS_ATTENTION,
                organization_mode=OrganizationMode.PACK,
                operation_token="organize-token",
            )
            session.add(job)
            session.commit()
            ledger = OrganizedFile(
                job_id=job.id,
                path="/library/Show - S01E01.mkv",
                lifecycle=FileLifecycle.PUBLISHED,
                operation_token="organize-token",
                published_device=1,
                published_inode=1,
            )
            session.add(ledger)
            session.commit()
            job_id = job.id
            ledger_id = ledger.id

        from skald.lifecycle import try_job_lock

        with try_job_lock(job_id) as held:
            assert held
            response = client.post(
                f"/jobs/{job_id}/retry",
                data={"title": "Show", "season": 1, "episode": 1},
                follow_redirects=False,
            )

        assert response.status_code == 409
        assert "already being updated" in response.text
        with Session(app.state.engine) as session:
            job = session.get(MediaJob, job_id)
            assert job.status == JobStatus.NEEDS_ATTENTION
            assert job.operation_token == "organize-token"
            assert session.get(OrganizedFile, ledger_id).lifecycle == FileLifecycle.PUBLISHED


def test_retry_pack_to_scalar_clears_only_nonlegacy_ledger_rows_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "retry-pack-to-scalar.db"))
    app = create_app()

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV, title="Show", season=1, episode=1,
                release_title="Show.S01", qbit_hash="hash", category="skald-tv",
                status=JobStatus.NEEDS_ATTENTION,
                organization_mode=OrganizationMode.PACK,
                operation_token="organize-token",
            )
            other_job = MediaJob(
                type=MediaType.TV, title="Other", season=1, episode=1,
                release_title="Other.S01", qbit_hash="other-hash", category="skald-tv",
                status=JobStatus.NEEDS_ATTENTION,
                organization_mode=OrganizationMode.PACK,
                operation_token="other-token",
            )
            session.add_all([job, other_job])
            session.commit()
            normal_row = OrganizedFile(
                job_id=job.id, path="/library/Show - S01E01.mkv",
                lifecycle=FileLifecycle.PUBLISHED, operation_token="organize-token",
            )
            legacy_row = OrganizedFile(
                job_id=job.id, path="/library/Show - S01E02.mkv",
                lifecycle=FileLifecycle.LEGACY_UNVERIFIED,
            )
            other_row = OrganizedFile(
                job_id=other_job.id, path="/library/Other - S01E01.mkv",
                lifecycle=FileLifecycle.PUBLISHED, operation_token="other-token",
            )
            session.add_all([normal_row, legacy_row, other_row])
            session.commit()
            job_id = job.id
            normal_id = normal_row.id
            legacy_id = legacy_row.id
            other_id = other_row.id

        response = client.post(
            f"/jobs/{job_id}/retry",
            data={"title": "Show", "season": 1, "episode": 1},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with Session(app.state.engine) as session:
        retried = session.get(MediaJob, job_id)
        assert retried.organization_mode == OrganizationMode.SCALAR
        assert retried.operation_token is None
        assert retried.status == JobStatus.ORGANIZING
        assert session.get(OrganizedFile, normal_id) is None
        assert session.get(OrganizedFile, legacy_id) is not None
        assert session.get(OrganizedFile, other_id) is not None


def test_retry_pack_to_scalar_rolls_back_ledger_mode_and_token_on_commit_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "retry-pack-rollback.db"))
    app = create_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        with Session(app.state.engine) as session:
            job = MediaJob(
                type=MediaType.TV, title="Show", season=1, episode=1,
                release_title="Show.S01", qbit_hash="hash", category="skald-tv",
                status=JobStatus.NEEDS_ATTENTION,
                organization_mode=OrganizationMode.PACK,
                operation_token="organize-token",
            )
            session.add(job)
            session.commit()
            normal_row = OrganizedFile(
                job_id=job.id, path="/library/Show - S01E01.mkv",
                lifecycle=FileLifecycle.PUBLISHED, operation_token="organize-token",
            )
            legacy_row = OrganizedFile(
                job_id=job.id, path="/library/Show - S01E02.mkv",
                lifecycle=FileLifecycle.LEGACY_UNVERIFIED,
            )
            session.add_all([normal_row, legacy_row])
            session.commit()
            job_id = job.id
            normal_id = normal_row.id
            legacy_id = legacy_row.id

        original_commit = Session.commit

        def fail_retry_commit(session):
            if session.get(MediaJob, job_id) is not None:
                raise RuntimeError("commit failed")
            return original_commit(session)

        monkeypatch.setattr(Session, "commit", fail_retry_commit)
        response = client.post(
            f"/jobs/{job_id}/retry",
            data={"title": "Show", "season": 1, "episode": 1},
            follow_redirects=False,
        )

    assert response.status_code == 500
    with Session(app.state.engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.organization_mode == OrganizationMode.PACK
        assert job.operation_token == "organize-token"
        assert session.get(OrganizedFile, normal_id) is not None
        assert session.get(OrganizedFile, legacy_id) is not None
