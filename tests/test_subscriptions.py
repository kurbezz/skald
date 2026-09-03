from datetime import UTC, datetime, timedelta

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from skald.db import get_engine, migrate_schema
from skald.indexer.base import ReleaseResult
from skald.config import Settings
from skald.main import create_app
from skald.models import (
    MediaJob,
    MediaSubscription,
    MediaType,
    SubscriptionRelease,
    SubscriptionReleaseScope,
    TvSubscriptionScope,
)
from skald.qbittorrent import TorrentFile
from skald.routes import subscriptions as subscription_routes
from skald.subscriptions import (
    matching_tv_subscription_scopes,
    scan_due_subscriptions,
    tv_scope_matches_release,
)
from skald.tmdb import TmdbEpisode, TmdbError, TmdbMedia, TmdbSeason, TmdbTvSeason


class ScanIndexer:
    def __init__(self, releases: list[ReleaseResult], failures: set[str] | None = None):
        self.releases = releases
        self.failures = failures or set()
        self.queries: list[str] = []

    async def search(self, query: str) -> list[ReleaseResult]:
        self.queries.append(query)
        if query in self.failures:
            raise RuntimeError(f"indexer failed for {query}")
        return self.releases


class RecordingQbit:
    def __init__(self, failures: int = 0):
        self.add_calls: list[tuple[str, str]] = []
        self.failures = failures

    def add_torrent(self, download_url: str, category: str) -> str:
        self.add_calls.append((download_url, category))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("qBittorrent unavailable")
        return "auto-grab-hash"


class SelectiveRecordingQbit(RecordingQbit):
    def __init__(self, files: list[TorrentFile]):
        super().__init__()
        self.files = files
        self.paused_add_calls: list[tuple[str, str]] = []
        self.priority_calls: list[tuple[str, list[int], int]] = []
        self.resumed: list[str] = []

    def add_torrent_paused(self, download_url: str, category: str) -> str:
        self.paused_add_calls.append((download_url, category))
        return "targeted-auto-grab-hash"

    def get_torrent_files(self, torrent_hash: str) -> list[TorrentFile]:
        return self.files

    def set_file_priority(self, torrent_hash: str, file_indexes: list[int], priority: int) -> None:
        self.priority_calls.append((torrent_hash, file_indexes, priority))

    def resume_torrent(self, torrent_hash: str) -> None:
        self.resumed.append(torrent_hash)


async def test_tv_scopes_persist_series_season_and_episode_coordinates(session):
    subscription = MediaSubscription(tmdb_id=1396, type=MediaType.TV, title="Breaking Bad")
    session.add(subscription)
    session.commit()
    session.refresh(subscription)
    series = TvSubscriptionScope(
        subscription_id=subscription.id,
        tmdb_series_id=1396,
        includes_future_content=True,
    )
    season = TvSubscriptionScope(
        subscription_id=subscription.id,
        tmdb_series_id=1396,
        tmdb_season_id=3577,
        season_number=2,
    )
    episode = TvSubscriptionScope(
        subscription_id=subscription.id,
        tmdb_series_id=1396,
        tmdb_season_id=3577,
        tmdb_episode_id=62085,
        season_number=2,
        episode_number=3,
    )
    session.add_all([series, season, episode])
    session.commit()

    scopes = session.exec(
        select(TvSubscriptionScope)
        .where(TvSubscriptionScope.subscription_id == subscription.id)
        .order_by(TvSubscriptionScope.id)
    ).all()

    assert [(scope.tmdb_season_id, scope.tmdb_episode_id) for scope in scopes] == [
        (None, None), (3577, None), (3577, 62085)
    ]
    assert scopes[0].includes_future_content is True
    assert (scopes[2].season_number, scopes[2].episode_number) == (2, 3)


def test_migration_creates_durable_tv_scope_tables_for_existing_databases(tmp_path):
    engine = get_engine(str(tmp_path / "legacy-subscriptions.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE mediasubscription (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE subscriptionrelease (id INTEGER PRIMARY KEY)")

    migrate_schema(engine)

    with engine.connect() as connection:
        scope_columns = {
            column[1]
            for column in connection.exec_driver_sql("PRAGMA table_info(tvsubscriptionscope)").fetchall()
        }
        target_columns = {
            column[1]
            for column in connection.exec_driver_sql("PRAGMA table_info(subscriptionreleasescope)").fetchall()
        }

    assert scope_columns == {
        "id", "subscription_id", "tmdb_series_id", "tmdb_season_id", "tmdb_episode_id",
        "season_number", "episode_number", "includes_future_content",
    }
    assert target_columns == {"id", "subscription_release_id", "tv_subscription_scope_id"}


@pytest.mark.parametrize(
    ("scope", "release_title", "matches"),
    [
        pytest.param(
            TvSubscriptionScope(subscription_id=1, tmdb_series_id=1396, includes_future_content=True),
            "Breaking.Bad.S05E16.1080p.WEB",
            True,
            id="series-mode-includes-future-content",
        ),
        pytest.param(
            TvSubscriptionScope(
                subscription_id=1, tmdb_series_id=1396, tmdb_season_id=3577, season_number=2
            ),
            "Breaking.Bad.S02.COMPLETE.1080p.WEB",
            True,
            id="selected-season-matches-season-pack",
        ),
        pytest.param(
            TvSubscriptionScope(
                subscription_id=1, tmdb_series_id=1396, tmdb_season_id=3577, season_number=2
            ),
            "Breaking.Bad.S01E07.1080p.WEB",
            False,
            id="selected-season-excludes-other-seasons",
        ),
        pytest.param(
            TvSubscriptionScope(
                subscription_id=1,
                tmdb_series_id=1396,
                tmdb_season_id=3577,
                tmdb_episode_id=62085,
                season_number=2,
                episode_number=3,
            ),
            "Breaking.Bad.S02E03.1080p.WEB",
            True,
            id="exact-episode-matches-single-episode",
        ),
        pytest.param(
            TvSubscriptionScope(
                subscription_id=1,
                tmdb_series_id=1396,
                tmdb_season_id=3577,
                tmdb_episode_id=62085,
                season_number=2,
                episode_number=3,
            ),
            "Breaking.Bad.S02E02-E04.1080p.WEB",
            True,
            id="exact-episode-matches-containing-pack",
        ),
        pytest.param(
            TvSubscriptionScope(
                subscription_id=1,
                tmdb_series_id=1396,
                tmdb_season_id=3577,
                tmdb_episode_id=62085,
                season_number=2,
                episode_number=3,
            ),
            "Breaking.Bad.S02E04.1080p.WEB",
            False,
            id="exact-episode-excludes-unrequested-episode",
        ),
        pytest.param(
            TvSubscriptionScope(
                subscription_id=1,
                tmdb_series_id=1396,
                tmdb_season_id=3575,
                tmdb_episode_id=62001,
                season_number=0,
                episode_number=2,
            ),
            "Breaking.Bad.S00E02.1080p.WEB",
            True,
            id="specials-use-season-zero",
        ),
    ],
)
def test_tv_scope_matching_uses_normalized_parser_episode_sets(scope, release_title, matches):
    assert tv_scope_matches_release(scope, release_title) is matches


async def test_matching_tv_scopes_and_release_targets_are_durable(session):
    subscription = MediaSubscription(tmdb_id=1396, type=MediaType.TV, title="Breaking Bad")
    session.add(subscription)
    session.commit()
    season = TvSubscriptionScope(
        subscription_id=subscription.id, tmdb_series_id=1396, tmdb_season_id=3577, season_number=2
    )
    episode = TvSubscriptionScope(
        subscription_id=subscription.id,
        tmdb_series_id=1396,
        tmdb_season_id=3577,
        tmdb_episode_id=62085,
        season_number=2,
        episode_number=3,
    )
    release = SubscriptionRelease(
        subscription_id=subscription.id,
        release_title="Breaking.Bad.S02E02-E04.1080p.WEB",
        indexer="fake",
        size_bytes=1,
        seeders=1,
        leechers=0,
        download_url="magnet:?pack",
        fingerprint="pack-targets",
    )
    session.add_all([season, episode, release])
    session.commit()
    session.add_all([
        SubscriptionReleaseScope(subscription_release_id=release.id, tv_subscription_scope_id=season.id),
        SubscriptionReleaseScope(subscription_release_id=release.id, tv_subscription_scope_id=episode.id),
    ])
    session.commit()

    assert matching_tv_subscription_scopes(
        session, subscription, release.release_title
    ) == [season, episode]
    assert len(session.exec(select(SubscriptionReleaseScope)).all()) == 2


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "subscriptions.db"))
    app = create_app()

    async with app.router.lifespan_context(app):
        with Session(app.state.engine) as database_session:
            yield database_session


async def test_media_subscription_tmdb_id_and_type_are_unique(session):
    session.add(MediaSubscription(tmdb_id=603, type=MediaType.MOVIE, title="The Matrix"))
    session.commit()
    session.add(MediaSubscription(tmdb_id=603, type=MediaType.MOVIE, title="The Matrix"))

    with pytest.raises(IntegrityError):
        session.commit()


async def test_subscription_release_fingerprints_are_unique(session):
    subscription = MediaSubscription(tmdb_id=603, type=MediaType.MOVIE, title="The Matrix")
    session.add(subscription)
    session.commit()
    session.add_all([
        SubscriptionRelease(
            subscription_id=subscription.id,
            release_title="The.Matrix.1999",
            indexer="fake",
            size_bytes=1,
            seeders=1,
            leechers=0,
            download_url="magnet:?one",
            fingerprint="same",
        ),
        SubscriptionRelease(
            subscription_id=subscription.id,
            release_title="The.Matrix.1999",
            indexer="fake",
            size_bytes=1,
            seeders=1,
            leechers=0,
            download_url="magnet:?one",
            fingerprint="same",
        ),
    ])

    with pytest.raises(IntegrityError):
        session.commit()


async def test_due_scan_records_first_result_then_deduplicates(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=603,
        type=MediaType.MOVIE,
        title="The Matrix",
        year=1999,
        next_check_at=now,
    )
    session.add(subscription)
    session.commit()
    indexer = ScanIndexer([
        ReleaseResult("The.Matrix.1999.1080p", "fake", 1, 2, 3, "magnet:?one")
    ])

    await scan_due_subscriptions(session, indexer, interval_seconds=21_600, now=now)
    subscription.next_check_at = now + timedelta(hours=6)
    session.add(subscription)
    session.commit()
    await scan_due_subscriptions(
        session, indexer, interval_seconds=21_600, now=now + timedelta(hours=6)
    )

    assert len(session.exec(select(SubscriptionRelease)).all()) == 1
    assert indexer.queries == ["The Matrix 1999", "The Matrix 1999"]
    assert session.exec(select(MediaJob)).all() == []


async def test_due_scan_skips_inactive_future_and_wrong_media_type(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    due = MediaSubscription(
        tmdb_id=1, type=MediaType.MOVIE, title="Movie", next_check_at=now
    )
    inactive = MediaSubscription(
        tmdb_id=2, type=MediaType.MOVIE, title="Inactive", is_active=False, next_check_at=now
    )
    future = MediaSubscription(
        tmdb_id=3,
        type=MediaType.MOVIE,
        title="Future",
        next_check_at=now + timedelta(seconds=1),
    )
    session.add_all([due, inactive, future])
    session.commit()
    indexer = ScanIndexer([
        ReleaseResult("Show.S01E01.1080p", "fake", 1, 2, 3, "magnet:?tv"),
        ReleaseResult("Movie.2020.1080p", "fake", 1, 2, 3, "magnet:?movie"),
    ])

    await scan_due_subscriptions(session, indexer, interval_seconds=60, now=now)

    assert indexer.queries == ["Movie"]
    releases = session.exec(select(SubscriptionRelease)).all()
    assert [release.release_title for release in releases] == ["Movie.2020.1080p"]


async def test_due_scan_records_one_error_and_continues_to_next_subscription(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    broken = MediaSubscription(
        tmdb_id=1, type=MediaType.MOVIE, title="Broken", next_check_at=now
    )
    working = MediaSubscription(
        tmdb_id=2, type=MediaType.MOVIE, title="Working", next_check_at=now
    )
    session.add_all([broken, working])
    session.commit()
    indexer = ScanIndexer(
        [ReleaseResult("Working.2020.1080p", "fake", 1, 2, 3, "magnet:?working")],
        failures={"Broken"},
    )

    await scan_due_subscriptions(session, indexer, interval_seconds=60, now=now)

    assert indexer.queries == ["Broken", "Working"]
    session.refresh(broken)
    session.refresh(working)
    assert broken.last_error == "indexer failed for Broken"
    assert broken.next_check_at.replace(tzinfo=UTC) == now + timedelta(seconds=60)
    assert working.last_error is None
    assert working.last_checked_at.replace(tzinfo=UTC) == now
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1


async def test_auto_download_persists_discoveries_then_grabs_highest_seed_matching_movie(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1,
        type=MediaType.MOVIE,
        title="Movie",
        year=2026,
        auto_download=True,
        next_check_at=now,
    )
    session.add(subscription)
    session.commit()
    qbit = RecordingQbit()
    indexer = ScanIndexer([
        ReleaseResult("Movie.2026.720p.WEB", "fake", 1, 99, 0, "magnet:?ignored"),
        ReleaseResult("Movie.2026.1080p.WEB", "fake", 1, 6, 0, "magnet:?good"),
        ReleaseResult("Movie.2026.2160p.WEB", "fake", 1, 8, 0, "magnet:?best"),
    ])

    await scan_due_subscriptions(
        session,
        indexer,
        qbit=qbit,
        settings=Settings(category_movie="skald-movie"),
        interval_seconds=60,
        now=now,
    )

    session.refresh(subscription)
    selected = session.get(SubscriptionRelease, subscription.auto_grabbed_release_id)
    assert qbit.add_calls == [("magnet:?best", "skald-movie")]
    assert selected.release_title == "Movie.2026.2160p.WEB"
    assert len(session.exec(select(MediaJob)).all()) == 1


async def test_auto_download_never_grabs_tv(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title="Show", auto_download=True, next_check_at=now
    )
    session.add(subscription)
    session.commit()
    qbit = RecordingQbit()

    await scan_due_subscriptions(
        session,
        ScanIndexer([ReleaseResult("Show.S01E01.1080p", "fake", 1, 99, 0, "magnet:?tv")]),
        qbit=qbit,
        settings=Settings(),
        interval_seconds=60,
        now=now,
    )

    session.refresh(subscription)
    assert qbit.add_calls == []
    assert subscription.auto_grabbed_release_id is None


async def test_scoped_tv_auto_download_persists_matching_scope_and_grabs_only_required_files(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title="Show", auto_download=True, next_check_at=now
    )
    session.add(subscription)
    session.commit()
    scope = TvSubscriptionScope(
        subscription_id=subscription.id,
        tmdb_series_id=1,
        tmdb_season_id=10,
        tmdb_episode_id=103,
        season_number=1,
        episode_number=3,
    )
    session.add(scope)
    session.commit()
    qbit = SelectiveRecordingQbit([
        TorrentFile(index=2, name="Show.S01E02.mkv"),
        TorrentFile(index=9, name="Show.S01E03.mkv"),
        TorrentFile(index=15, name="Show.S01E04.mkv"),
    ])

    await scan_due_subscriptions(
        session,
        ScanIndexer([
            ReleaseResult("Show.S01E02.1080p.WEB", "fake", 1, 99, 0, "magnet:?other"),
            ReleaseResult("Show.S01E02-E04.1080p.WEB", "fake", 1, 5, 0, "magnet:?pack"),
        ]),
        qbit=qbit,
        settings=Settings(category_tv="tv"),
        interval_seconds=60,
        now=now,
    )

    session.refresh(subscription)
    releases = session.exec(select(SubscriptionRelease)).all()
    assert [release.release_title for release in releases] == ["Show.S01E02-E04.1080p.WEB"]
    assert [target.tv_subscription_scope_id for target in session.exec(select(SubscriptionReleaseScope)).all()] == [scope.id]
    assert qbit.add_calls == []
    assert qbit.paused_add_calls == [("magnet:?pack", "tv")]
    assert qbit.priority_calls == [
        ("targeted-auto-grab-hash", [2, 9, 15], 0),
        ("targeted-auto-grab-hash", [9], 1),
    ]
    assert qbit.resumed == ["targeted-auto-grab-hash"]
    job = session.exec(select(MediaJob)).one()
    assert (job.type, job.season, job.episode, job.episode_set) == (
        MediaType.TV, 1, 3, "[3]"
    )
    assert subscription.auto_grabbed_release_id == releases[0].id


async def test_scoped_tv_auto_download_keeps_missing_target_paused_and_retryable(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title="Show", auto_download=True, next_check_at=now
    )
    session.add(subscription)
    session.commit()
    session.add(TvSubscriptionScope(
        subscription_id=subscription.id,
        tmdb_series_id=1,
        tmdb_season_id=10,
        tmdb_episode_id=103,
        season_number=1,
        episode_number=3,
    ))
    session.commit()
    qbit = SelectiveRecordingQbit([TorrentFile(index=4, name="Show.S01E02.mkv")])

    await scan_due_subscriptions(
        session,
        ScanIndexer([ReleaseResult("Show.S01E03.1080p.WEB", "fake", 1, 5, 0, "magnet:?one")]),
        qbit=qbit,
        settings=Settings(),
        interval_seconds=60,
        now=now,
    )

    session.refresh(subscription)
    assert qbit.paused_add_calls == [("magnet:?one", Settings().category_tv)]
    assert qbit.priority_calls == []
    assert qbit.resumed == []
    assert session.exec(select(MediaJob)).all() == []
    assert subscription.auto_grabbed_release_id is None
    assert subscription.last_error.startswith("Automatic grab failed:")


async def test_auto_download_skips_disabled_and_non_matching_movies(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    disabled = MediaSubscription(
        tmdb_id=1, type=MediaType.MOVIE, title="Disabled", next_check_at=now
    )
    no_match = MediaSubscription(
        tmdb_id=2,
        type=MediaType.MOVIE,
        title="No Match",
        auto_download=True,
        next_check_at=now,
    )
    session.add_all([disabled, no_match])
    session.commit()
    qbit = RecordingQbit()
    indexer = ScanIndexer([ReleaseResult("Movie.2026.720p.WEB", "fake", 1, 99, 0, "magnet:?no")])

    await scan_due_subscriptions(
        session, indexer, qbit=qbit, settings=Settings(), interval_seconds=60, now=now
    )

    assert qbit.add_calls == []
    assert session.exec(select(MediaJob)).all() == []


async def test_auto_download_failure_is_isolated_retryable_and_never_duplicates_after_success(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1,
        type=MediaType.MOVIE,
        title="Movie",
        auto_download=True,
        next_check_at=now,
    )
    session.add(subscription)
    session.commit()
    qbit = RecordingQbit(failures=1)
    indexer = ScanIndexer([ReleaseResult("Movie.2026.1080p.WEB", "fake", 1, 5, 0, "magnet:?one")])

    await scan_due_subscriptions(
        session, indexer, qbit=qbit, settings=Settings(), interval_seconds=60, now=now
    )

    session.refresh(subscription)
    assert subscription.auto_grabbed_release_id is None
    assert "qBittorrent unavailable" in subscription.last_error
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1

    retry_at = now + timedelta(seconds=60)
    await scan_due_subscriptions(
        session, indexer, qbit=qbit, settings=Settings(), interval_seconds=60, now=retry_at
    )
    session.refresh(subscription)
    assert subscription.auto_grabbed_release_id is not None
    assert subscription.last_error is None
    assert len(session.exec(select(MediaJob)).all()) == 1

    await scan_due_subscriptions(
        session,
        indexer,
        qbit=qbit,
        settings=Settings(),
        interval_seconds=60,
        now=retry_at + timedelta(seconds=60),
    )
    assert qbit.add_calls == [("magnet:?one", "skald-movie"), ("magnet:?one", "skald-movie")]
    assert len(session.exec(select(MediaJob)).all()) == 1


class FakeTmdb:
    def __init__(self, results=None, media=None, seasons=None, season=None, error=None, configured=True):
        self.results = results or []
        self.media = media
        self.seasons = seasons or []
        self.season = season
        self.error = error
        self.configured = configured
        self.search_queries = []
        self.media_requests = []
        self.seasons_requests = []
        self.season_requests = []
        self.closed = False

    async def search(self, query):
        self.search_queries.append(query)
        if self.error:
            raise self.error
        return self.results

    async def get_media(self, tmdb_id, media_type):
        self.media_requests.append((tmdb_id, media_type))
        if self.error:
            raise self.error
        return self.media

    async def get_tv_seasons(self, tmdb_id):
        self.seasons_requests.append(tmdb_id)
        if self.error:
            raise self.error
        return self.seasons

    async def get_tv_season(self, tmdb_id, season_number):
        self.season_requests.append((tmdb_id, season_number))
        if self.error:
            raise self.error
        return self.season

    async def aclose(self):
        self.closed = True


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "subscription-routes.db"))
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def captured_templates(monkeypatch):
    captured = []

    def capture(request, name, context, status_code=200):
        captured.append((name, context, status_code))
        return HTMLResponse("captured", status_code=status_code)

    monkeypatch.setattr(subscription_routes.templates, "TemplateResponse", capture)
    return captured


def test_catalog_search_and_create_subscription(client, app, captured_templates):
    matrix = TmdbMedia(603, MediaType.MOVIE, "The Matrix", "The Matrix", 1999, None)
    tmdb = FakeTmdb(results=[matrix], media=matrix)
    app.state.tmdb = tmdb

    response = client.get("/subscriptions", params={"q": "matrix"})
    created = client.post(
        "/subscriptions",
        data={"tmdb_id": 603, "media_type": "movie", "title": "Untrusted title"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert captured_templates[-1][0] == "subscriptions.html"
    assert captured_templates[-1][1]["catalog_results"] == [matrix]
    assert tmdb.search_queries == ["matrix"]
    assert created.status_code == 303
    assert created.headers["location"] == "/subscriptions"
    assert tmdb.media_requests == [(603, MediaType.MOVIE)]
    with Session(app.state.engine) as database_session:
        subscription = database_session.exec(select(MediaSubscription)).one()
    assert (subscription.title, subscription.original_title, subscription.year) == (
        "The Matrix", "The Matrix", 1999,
    )


def test_subscriptions_page_renders_catalog_subscription_and_recent_release(client, app):
    matrix = TmdbMedia(603, MediaType.MOVIE, "The Matrix", "The Matrix", 1999, None)
    app.state.tmdb = FakeTmdb(results=[matrix])
    with Session(app.state.engine) as database_session:
        subscription = MediaSubscription(
            tmdb_id=603, type=MediaType.MOVIE, title="The Matrix"
        )
        database_session.add(subscription)
        database_session.commit()
        database_session.refresh(subscription)
        database_session.add(SubscriptionRelease(
            subscription_id=subscription.id,
            release_title="The.Matrix.1999.1080p",
            indexer="fake",
            size_bytes=1,
            seeders=2,
            leechers=3,
            download_url="magnet:?matrix",
            fingerprint="matrix-recent-release",
        ))
        database_session.commit()

    response = client.get("/subscriptions", params={"q": "matrix"})

    assert response.status_code == 200
    assert 'value="matrix"' in response.text
    assert "Catalog matches" in response.text
    assert "1 unread" in response.text
    assert "The.Matrix.1999.1080p" in response.text


def test_subscription_creation_is_idempotent_and_missing_media_is_404(client, app):
    matrix = TmdbMedia(603, MediaType.MOVIE, "The Matrix", "The Matrix", 1999, None)
    app.state.tmdb = FakeTmdb(media=matrix)

    assert client.post(
        "/subscriptions", data={"tmdb_id": 603, "media_type": "movie"}, follow_redirects=False
    ).status_code == 303
    assert client.post(
        "/subscriptions", data={"tmdb_id": 603, "media_type": "movie"}, follow_redirects=False
    ).status_code == 303
    with Session(app.state.engine) as database_session:
        assert len(database_session.exec(select(MediaSubscription)).all()) == 1

    app.state.tmdb = FakeTmdb(media=None)
    assert client.post(
        "/subscriptions", data={"tmdb_id": 404, "media_type": "movie"}, follow_redirects=False
    ).status_code == 404


def test_catalog_surfaces_missing_tmdb_configuration_and_tmdb_errors(
    client, app, captured_templates
):
    app.state.tmdb = FakeTmdb(configured=False)
    assert client.get("/subscriptions", params={"q": "matrix"}).status_code == 200
    assert captured_templates[-1][1]["error"] == "TMDB is not configured"
    assert app.state.tmdb.search_queries == []

    app.state.tmdb = FakeTmdb(error=TmdbError("TMDB request failed"))
    assert client.get("/subscriptions", params={"q": "matrix"}).status_code == 200
    assert captured_templates[-1][1]["error"] == "TMDB request failed"


def test_subscription_actions_toggle_schedule_read_delete_and_404(client, app):
    with Session(app.state.engine) as database_session:
        subscription = MediaSubscription(
            tmdb_id=603, type=MediaType.MOVIE, title="The Matrix", is_active=False
        )
        database_session.add(subscription)
        database_session.commit()
        database_session.refresh(subscription)
        subscription_id = subscription.id
        database_session.add(SubscriptionRelease(
            subscription_id=subscription_id,
            release_title="The.Matrix.1999.1080p",
            indexer="fake",
            size_bytes=1,
            seeders=2,
            leechers=3,
            download_url="magnet:?one",
            fingerprint="matrix-release",
        ))
        database_session.commit()

    assert client.post(f"/subscriptions/{subscription_id}/toggle", follow_redirects=False).status_code == 303
    assert client.post(
        f"/subscriptions/{subscription_id}/releases/read", follow_redirects=False
    ).status_code == 303
    with Session(app.state.engine) as database_session:
        enabled = database_session.get(MediaSubscription, subscription_id)
        release = database_session.exec(select(SubscriptionRelease)).one()
        assert enabled.is_active is True
        assert enabled.next_check_at is not None
        assert release.read_at is not None

    assert client.post(f"/subscriptions/{subscription_id}/delete", follow_redirects=False).status_code == 303
    with Session(app.state.engine) as database_session:
        assert database_session.get(MediaSubscription, subscription_id) is None
        assert database_session.exec(select(SubscriptionRelease)).all() == []

    for action in ("toggle", "releases/read", "delete"):
        assert client.post(f"/subscriptions/999/{action}", follow_redirects=False).status_code == 404


def test_tv_subscription_detail_and_season_routes(client, app, captured_templates):
    with Session(app.state.engine) as database_session:
        subscription = MediaSubscription(tmdb_id=1396, type=MediaType.TV, title="Breaking Bad")
        database_session.add(subscription)
        database_session.commit()
        database_session.refresh(subscription)
        database_session.add(TvSubscriptionScope(
            subscription_id=subscription.id,
            tmdb_series_id=1396,
            tmdb_season_id=3572,
            season_number=1,
        ))
        database_session.commit()
        subscription_id = subscription.id

    seasons = [TmdbSeason(3571, 0, "Specials", None, 10), TmdbSeason(3572, 1, "Season 1", None, 7)]
    season = TmdbTvSeason(
        3572,
        1,
        "Season 1",
        None,
        [TmdbEpisode(62001, 1, "Pilot", "2008-01-20")],
    )
    tmdb = FakeTmdb(seasons=seasons, season=season)
    app.state.tmdb = tmdb

    detail = client.get(f"/subscriptions/{subscription_id}")
    episodes = client.get(f"/subscriptions/{subscription_id}/seasons/1")

    assert detail.status_code == 200
    assert captured_templates[-1][0] == "subscription_detail.html"
    assert captured_templates[-1][1]["subscription"].id == subscription_id
    assert captured_templates[-1][1]["seasons"] == seasons
    assert captured_templates[-1][1]["scopes"][0].season_number == 1
    assert tmdb.seasons_requests == [1396]
    assert episodes.json() == {
        "tmdb_id": 3572,
        "season_number": 1,
        "name": "Season 1",
        "air_date": None,
        "episodes": [{
            "tmdb_id": 62001,
            "episode_number": 1,
            "name": "Pilot",
            "air_date": "2008-01-20",
        }],
    }
    assert tmdb.season_requests == [(1396, 1)]


def test_tv_detail_routes_reject_non_tv_missing_and_tmdb_failures(client, app, captured_templates):
    with Session(app.state.engine) as database_session:
        movie = MediaSubscription(tmdb_id=603, type=MediaType.MOVIE, title="The Matrix")
        tv = MediaSubscription(tmdb_id=1396, type=MediaType.TV, title="Breaking Bad")
        database_session.add_all([movie, tv])
        database_session.commit()
        database_session.refresh(movie)
        database_session.refresh(tv)

    app.state.tmdb = FakeTmdb(error=TmdbError("TMDB request failed"))

    assert client.get(f"/subscriptions/{movie.id}").status_code == 404
    assert client.get("/subscriptions/999").status_code == 404
    failed = client.get(f"/subscriptions/{tv.id}")
    assert failed.status_code == 502
    assert captured_templates[-1][1]["back_url"] == f"/subscriptions/{tv.id}"


def test_subscriptions_route_requires_configured_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "subscription-auth.db"))
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/subscriptions", follow_redirects=False)
        detail = client.get("/subscriptions/1", follow_redirects=False)
        season = client.get("/subscriptions/1/seasons/0", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/subscriptions"
    assert detail.status_code == 303
    assert detail.headers["location"] == "/login?next=/subscriptions/1"
    assert season.status_code == 303
    assert season.headers["location"] == "/login?next=/subscriptions/1/seasons/0"
