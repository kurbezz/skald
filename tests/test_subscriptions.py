from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus, urlencode

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from skald.auth import create_csrf_token
from skald.db import get_engine, migrate_schema
from skald.indexer.base import ReleaseResult
from skald.config import Settings
from skald.main import create_app
from skald.models import (
    DownloadedQuality,
    NotificationChannel,
    NotificationDeliveryAttempt,
    JobStatus,
    MediaJob,
    MediaSubscription,
    MediaType,
    SubscriptionRelease,
    SubscriptionReleaseScope,
    SubscriptionEvent,
    SubscriptionEventKind,
    TvSubscriptionScope,
)
from skald.qbittorrent import TorrentFile
from skald.routes import subscriptions as subscription_routes
from skald.subscriptions import (
    matching_tv_subscription_scopes,
    release_fingerprint,
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


async def test_due_scan_treats_concurrent_fingerprint_conflict_as_successful_deduplication(
    session, monkeypatch
):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=603,
        type=MediaType.MOVIE,
        title="The Matrix",
        year=1999,
        next_check_at=now,
    )
    release = ReleaseResult("The.Matrix.1999.1080p", "fake", 1, 2, 3, "magnet:?race")
    session.add(subscription)
    session.commit()
    fingerprint = release_fingerprint(subscription.id, release)
    session.add(SubscriptionRelease(
        subscription_id=subscription.id,
        release_title=release.title,
        indexer=release.indexer,
        size_bytes=release.size_bytes,
        seeders=release.seeders,
        leechers=release.leechers,
        download_url=release.download_url,
        fingerprint=fingerprint,
    ))
    session.commit()

    original_exec = session.exec
    stale_lookup_returned = False

    class EmptyResult:
        def first(self):
            return None

    def stale_first_release_lookup(statement, *args, **kwargs):
        nonlocal stale_lookup_returned
        entity = statement.column_descriptions[0].get("entity")
        if entity is SubscriptionRelease and not stale_lookup_returned:
            stale_lookup_returned = True
            return EmptyResult()
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(session, "exec", stale_first_release_lookup)

    await scan_due_subscriptions(
        session,
        ScanIndexer([release]),
        interval_seconds=60,
        now=now,
    )

    session.refresh(subscription)
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1
    assert session.exec(select(SubscriptionEvent)).all() == []
    assert subscription.last_error is None
    assert subscription.last_checked_at.replace(tzinfo=UTC) == now
    assert subscription.next_check_at.replace(tzinfo=UTC) == now + timedelta(seconds=60)


async def test_delivery_failure_does_not_stop_the_next_subscription_scan(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    session.add_all([
        MediaSubscription(tmdb_id=1, type=MediaType.MOVIE, title="First", next_check_at=now),
        MediaSubscription(tmdb_id=2, type=MediaType.MOVIE, title="Second", next_check_at=now),
    ])
    session.commit()

    class FailingFirstDelivery:
        def __init__(self):
            self.event_ids = []

        def deliver_event(self, event_id):
            self.event_ids.append(event_id)
            if len(self.event_ids) == 1:
                raise RuntimeError("provider unavailable")

    delivery = FailingFirstDelivery()
    indexer = ScanIndexer([
        ReleaseResult("Film.2026.1080p", "fake", 1, 5, 0, "magnet:?film")
    ])

    await scan_due_subscriptions(
        session,
        indexer,
        delivery_service=delivery,
        interval_seconds=60,
        now=now,
    )

    assert len(delivery.event_ids) == 2
    assert len(session.exec(select(SubscriptionRelease)).all()) == 2
    assert len(session.exec(select(SubscriptionEvent)).all()) == 2


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
    job = session.exec(select(MediaJob)).one()
    assert (job.source_subscription_id, job.source_subscription_release_id) == (
        subscription.id, selected.id
    )


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
    assert (job.source_subscription_id, job.source_subscription_release_id) == (
        subscription.id, releases[0].id
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


class FlakyTvQbit(SelectiveRecordingQbit):
    def __init__(self, files, fail_urls=()):
        super().__init__(files)
        self.fail_urls = set(fail_urls)

    def add_torrent_paused(self, download_url, category):
        self.paused_add_calls.append((download_url, category))
        if download_url in self.fail_urls:
            raise RuntimeError("qBittorrent unavailable")
        return f"hash-{len(self.paused_add_calls)}"


def _tv_series_subscription(session, now):
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title="Show", auto_download=True, next_check_at=now
    )
    session.add(subscription)
    session.commit()
    session.add(TvSubscriptionScope(
        subscription_id=subscription.id, tmdb_series_id=1, includes_future_content=True
    ))
    session.commit()
    return subscription


_TV_FILES = [TorrentFile(index=i, name=f"Show.S01E0{i}.mkv") for i in range(1, 6)]


async def _tv_scan(session, qbit, releases, now):
    await scan_due_subscriptions(
        session,
        ScanIndexer(releases),
        qbit=qbit,
        settings=Settings(category_tv="tv"),
        interval_seconds=60,
        now=now,
    )


def _tv_job_sets(session):
    return sorted(
        (job.season, job.episode_set) for job in session.exec(select(MediaJob)).all()
    )


async def test_tv_auto_download_grabs_each_new_episode_across_scans(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _tv_series_subscription(session, now)
    qbit = SelectiveRecordingQbit(_TV_FILES)
    first = ReleaseResult("Show.S01E01.1080p.WEB", "fake", 1, 5, 0, "magnet:?e1")
    second = ReleaseResult("Show.S01E02.1080p.WEB", "fake", 1, 5, 0, "magnet:?e2")

    await _tv_scan(session, qbit, [first], now)
    assert _tv_job_sets(session) == [(1, "[1]")]

    await _tv_scan(session, qbit, [first, second], now + timedelta(seconds=60))
    assert _tv_job_sets(session) == [(1, "[1]"), (1, "[2]")]
    session.refresh(subscription)
    assert subscription.auto_grabbed_release_id is not None
    assert subscription.last_error is None

    await _tv_scan(session, qbit, [first, second], now + timedelta(seconds=120))
    assert len(session.exec(select(MediaJob)).all()) == 2


async def test_tv_auto_download_picks_best_rank_for_same_episode(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    _tv_series_subscription(session, now)
    qbit = SelectiveRecordingQbit(_TV_FILES)

    await _tv_scan(session, qbit, [
        ReleaseResult("Show.S01E03.720p.WEB", "fake", 1, 50, 0, "magnet:?low"),
        ReleaseResult("Show.S01E03.1080p.WEB", "fake", 1, 5, 0, "magnet:?high"),
    ], now)

    assert _tv_job_sets(session) == [(1, "[3]")]
    assert qbit.paused_add_calls == [("magnet:?high", "tv")]


async def test_tv_auto_download_skips_new_release_of_covered_episode(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    _tv_series_subscription(session, now)
    qbit = SelectiveRecordingQbit(_TV_FILES)
    await _tv_scan(session, qbit, [
        ReleaseResult("Show.S01E02.1080p.WEB", "fake", 1, 5, 0, "magnet:?old"),
    ], now)

    await _tv_scan(session, qbit, [
        ReleaseResult("Show.S01E02.1080p.WEB", "fake", 1, 5, 0, "magnet:?old"),
        ReleaseResult("Show.S01E02.2160p.WEB", "fake", 1, 5, 0, "magnet:?better"),
    ], now + timedelta(seconds=60))

    assert _tv_job_sets(session) == [(1, "[2]")]
    assert qbit.paused_add_calls == [("magnet:?old", "tv")]


async def test_tv_auto_download_multi_episode_release_targets_only_uncovered(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    _tv_series_subscription(session, now)
    qbit = SelectiveRecordingQbit(_TV_FILES)
    await _tv_scan(session, qbit, [
        ReleaseResult("Show.S01E04.1080p.WEB", "fake", 1, 5, 0, "magnet:?e4"),
    ], now)

    await _tv_scan(session, qbit, [
        ReleaseResult("Show.S01E04.1080p.WEB", "fake", 1, 5, 0, "magnet:?e4"),
        ReleaseResult("Show.S01E04-E05.1080p.WEB", "fake", 1, 5, 0, "magnet:?pack"),
    ], now + timedelta(seconds=60))

    assert _tv_job_sets(session) == [(1, "[4]"), (1, "[5]")]
    assert [call for call in qbit.priority_calls if call[2] == 1][-1][1] == [5]


async def test_tv_auto_download_partial_failure_keeps_first_job_and_retries_second(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _tv_series_subscription(session, now)
    qbit = FlakyTvQbit(_TV_FILES, fail_urls={"magnet:?e3"})
    releases = [
        ReleaseResult("Show.S01E02.1080p.WEB", "fake", 1, 9, 0, "magnet:?e2"),
        ReleaseResult("Show.S01E03.1080p.WEB", "fake", 1, 5, 0, "magnet:?e3"),
    ]

    await _tv_scan(session, qbit, releases, now)

    session.refresh(subscription)
    assert _tv_job_sets(session) == [(1, "[2]")]
    assert subscription.last_error.startswith("Automatic grab failed:")

    qbit.fail_urls.clear()
    await _tv_scan(session, qbit, releases, now + timedelta(seconds=60))

    session.refresh(subscription)
    assert _tv_job_sets(session) == [(1, "[2]"), (1, "[3]")]
    assert subscription.last_error is None
    assert [url for url, _ in qbit.paused_add_calls].count("magnet:?e2") == 1


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


def test_release_fingerprint_prefers_info_hash_then_guid_then_url():
    def make(url, guid=None, info_hash=None):
        return ReleaseResult("T", "idx", 1, 1, 0, url, guid=guid, info_hash=info_hash)

    assert release_fingerprint(1, make("u1", "g", "AA")) == release_fingerprint(1, make("u2", "g2", "aa"))
    assert release_fingerprint(1, make("u1", "g")) == release_fingerprint(1, make("u2", "g"))
    assert release_fingerprint(1, make("u1", "g")) != release_fingerprint(1, make("u1", "g2"))
    assert release_fingerprint(1, make("u1")) != release_fingerprint(1, make("u2"))
    assert release_fingerprint(1, make("u1", "g")) != release_fingerprint(2, make("u1", "g"))


async def test_rescan_with_rotated_download_url_does_not_duplicate_release_event_or_job(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=603, type=MediaType.MOVIE, title="The Matrix", year=1999,
        auto_download=True, next_check_at=now,
    )
    session.add(subscription)
    session.commit()
    qbit = RecordingQbit()

    def indexer_with(url):
        return ScanIndexer([ReleaseResult(
            "The.Matrix.1999.1080p.WEB", "jackett", 1, 5, 0, url,
            guid="https://tracker.example/details?id=1",
        )])

    await scan_due_subscriptions(
        session, indexer_with("http://j/dl/?path=AAA"), qbit=qbit, settings=Settings(),
        interval_seconds=60, now=now,
    )
    events_after_first = len(session.exec(select(SubscriptionEvent)).all())
    await scan_due_subscriptions(
        session, indexer_with("http://j/dl/?path=BBB"), qbit=qbit, settings=Settings(),
        interval_seconds=60, now=now + timedelta(seconds=60),
    )

    session.refresh(subscription)
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1
    assert len(session.exec(select(SubscriptionEvent)).all()) == events_after_first >= 1
    assert len(session.exec(select(MediaJob)).all()) == 1
    assert qbit.add_calls == [("http://j/dl/?path=AAA", "skald-movie")]
    assert subscription.auto_grabbed_release_id is not None


def _tv_pack_qbit():
    return SelectiveRecordingQbit([
        TorrentFile(index=i, name=f"Show.S01E{i:02d}.mkv") for i in range(1, 9)
    ])


def _tv_pack_indexer():
    return ScanIndexer([
        ReleaseResult("Show.S01E01-E08.1080p.WEB", "fake", 1, 73, 0, "magnet:?pack", guid="g1"),
    ])


async def _scan(session, indexer, qbit, now):
    await scan_due_subscriptions(
        session, indexer, qbit=qbit, settings=Settings(category_tv="tv"),
        interval_seconds=60, now=now,
    )


async def test_enabling_auto_download_after_discovery_grabs_stored_tv_release(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title="Show", auto_download=False, next_check_at=now
    )
    session.add(subscription)
    session.commit()
    session.add(TvSubscriptionScope(
        subscription_id=subscription.id, tmdb_series_id=1, includes_future_content=True
    ))
    session.commit()
    qbit, indexer = _tv_pack_qbit(), _tv_pack_indexer()

    await _scan(session, indexer, qbit, now)
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1
    assert session.exec(select(MediaJob)).all() == []

    subscription.auto_download = True
    subscription.next_check_at = now
    session.add(subscription)
    session.commit()
    await _scan(session, indexer, qbit, now + timedelta(seconds=1))

    jobs = session.exec(select(MediaJob)).all()
    assert len(jobs) == 1
    assert len(qbit.paused_add_calls) == 1
    assert qbit.priority_calls[1][1] == list(range(1, 9))
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1


async def test_scope_change_after_discovery_grabs_tv_release_and_repeat_scans_do_not_regrab(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title="Show", auto_download=True, next_check_at=now
    )
    session.add(subscription)
    session.commit()
    scope = TvSubscriptionScope(
        subscription_id=subscription.id, tmdb_series_id=1, tmdb_season_id=20, season_number=2
    )
    session.add(scope)
    session.commit()
    qbit, indexer = _tv_pack_qbit(), _tv_pack_indexer()

    await _scan(session, indexer, qbit, now)
    assert qbit.paused_add_calls == []

    session.delete(scope)
    session.add(TvSubscriptionScope(
        subscription_id=subscription.id, tmdb_series_id=1, tmdb_season_id=10, season_number=1
    ))
    subscription.next_check_at = now
    session.add(subscription)
    session.commit()
    await _scan(session, indexer, qbit, now + timedelta(seconds=1))
    assert len(qbit.paused_add_calls) == 1
    assert len(session.exec(select(MediaJob)).all()) == 1

    for step in (2, 3):
        subscription.next_check_at = now
        session.add(subscription)
        session.commit()
        await _scan(session, indexer, qbit, now + timedelta(seconds=step))
    assert len(qbit.paused_add_calls) == 1
    assert len(session.exec(select(MediaJob)).all()) == 1


async def test_enabling_auto_download_after_discovery_grabs_movie_once(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=603, type=MediaType.MOVIE, title="The Matrix", year=1999,
        auto_download=False, next_check_at=now,
    )
    session.add(subscription)
    session.commit()
    qbit = RecordingQbit()
    indexer = ScanIndexer([
        ReleaseResult("The.Matrix.1999.1080p.WEB", "fake", 1, 5, 0, "magnet:?matrix")
    ])

    await _scan(session, indexer, qbit, now)
    assert qbit.add_calls == []

    subscription.auto_download = True
    subscription.next_check_at = now
    session.add(subscription)
    session.commit()
    for step in (1, 2):
        await _scan(session, indexer, qbit, now + timedelta(seconds=step))
        subscription.next_check_at = now
        session.add(subscription)
        session.commit()

    assert qbit.add_calls == [("magnet:?matrix", "skald-movie")]
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
        data={"csrf_token": create_csrf_token(None), "tmdb_id": 603, "media_type": "movie", "title": "Untrusted title"},
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


def test_tv_create_redirects_to_detail_and_duplicate_redirects_to_existing(client, app):
    show = TmdbMedia(1396, MediaType.TV, "Breaking Bad", "Breaking Bad", 2008, None)
    app.state.tmdb = FakeTmdb(media=show)

    first = client.post(
        "/subscriptions", data={"csrf_token": create_csrf_token(None), "tmdb_id": 1396, "media_type": "tv"}, follow_redirects=False
    )
    second = client.post(
        "/subscriptions", data={"csrf_token": create_csrf_token(None), "tmdb_id": 1396, "media_type": "tv"}, follow_redirects=False
    )

    with Session(app.state.engine) as database_session:
        subscription = database_session.exec(select(MediaSubscription)).one()
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == f"/subscriptions/{subscription.id}?setup=1"
    assert second.headers["location"] == f"/subscriptions/{subscription.id}?setup=1"


def test_subscription_list_scope_summaries(client, app, captured_templates):
    def scope(subscription_id, **kwargs):
        return TvSubscriptionScope(subscription_id=subscription_id, tmdb_series_id=1, **kwargs)

    with Session(app.state.engine) as db:
        movie = MediaSubscription(tmdb_id=603, type=MediaType.MOVIE, title="Matrix")
        subs = [
            MediaSubscription(tmdb_id=10 + i, type=MediaType.TV, title=f"Show {i}")
            for i in range(6)
        ]
        db.add_all([movie, *subs])
        db.commit()
        ids = [s.id for s in subs]
        db.add_all([
            scope(ids[1], includes_future_content=True),
            scope(ids[2], tmdb_season_id=1, season_number=0),
            scope(ids[2], tmdb_season_id=2, season_number=2),
            scope(ids[3], tmdb_season_id=1, season_number=1),
            scope(ids[3], tmdb_season_id=3, season_number=3),
            scope(ids[3], tmdb_season_id=2, season_number=2, tmdb_episode_id=21, episode_number=1),
            scope(ids[3], tmdb_season_id=2, season_number=2, tmdb_episode_id=22, episode_number=2),
            scope(ids[3], tmdb_season_id=2, season_number=2, tmdb_episode_id=23, episode_number=3),
            scope(ids[4], tmdb_season_id=1, season_number=1, tmdb_episode_id=11, episode_number=1),
            scope(ids[5], tmdb_season_id=1, season_number=1),
        ])
        db.commit()
        movie_id = movie.id

    client.get("/subscriptions")

    summaries = captured_templates[-1][1]["scope_summaries"]
    assert movie_id not in summaries
    assert summaries[ids[0]] == {"state": "none", "label": ""}
    assert summaries[ids[1]] == {"state": "series", "label": "Entire series"}
    assert summaries[ids[2]] == {"state": "custom", "label": "Specials, Season 2"}
    assert summaries[ids[3]] == {"state": "custom", "label": "Seasons 1, 3 · 3 episodes"}
    assert summaries[ids[4]] == {"state": "custom", "label": "1 episode"}
    assert summaries[ids[5]] == {"state": "custom", "label": "Season 1"}
    assert set(summaries) == set(ids)


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


def test_events_history_renders_delivery_audit_and_manual_search_link(client, app):
    with Session(app.state.engine) as database_session:
        subscription = MediaSubscription(
            tmdb_id=603, type=MediaType.MOVIE, title="The Matrix"
        )
        database_session.add(subscription)
        database_session.commit()
        release = SubscriptionRelease(
            subscription_id=subscription.id,
            release_title="The Matrix 1999 2160p",
            indexer="fake",
            size_bytes=1,
            seeders=9,
            leechers=0,
            download_url="magnet:?matrix-event",
            fingerprint="matrix-event-release",
        )
        database_session.add(release)
        database_session.commit()
        release_title = release.release_title
        database_session.add_all([
            SubscriptionEvent(
                subscription_id=subscription.id,
                subscription_release_id=release.id,
                media_type=MediaType.MOVIE,
                kind=SubscriptionEventKind.RELEASE_MATCH,
                dedupe_key="release:matrix-event-release",
                title="New movie release: The Matrix",
                body="The Matrix 1999 2160p (9 seeders)",
            ),
            SubscriptionEvent(
                subscription_id=subscription.id,
                subscription_release_id=release.id,
                media_type=MediaType.MOVIE,
                target_key="movie:tmdb:603",
                kind=SubscriptionEventKind.UPGRADE_PROPOSAL,
                dedupe_key="upgrade:movie:tmdb:603:matrix-event-release",
                title="Upgrade available",
                body="A sharper release is ready to review.",
                prior_quality_score=[3, 2, 2],
                current_quality_score=[4, 3, 3],
            ),
        ])
        database_session.commit()
        proposal = database_session.exec(
            select(SubscriptionEvent).where(
                SubscriptionEvent.kind == SubscriptionEventKind.UPGRADE_PROPOSAL
            )
        ).one()
        database_session.add(NotificationDeliveryAttempt(
            event_id=proposal.id,
            channel=NotificationChannel.EMAIL,
            outcome="sent",
            provider_message_id="message-1",
        ))
        database_session.commit()

    response = client.get("/events")

    assert response.status_code == 200
    assert f'name="csrf_token" value="{create_csrf_token(None)}"' in response.text
    assert "New movie release: The Matrix" in response.text
    assert "Upgrade available" in response.text
    assert "release match" in response.text.lower()
    assert "upgrade proposal" in response.text.lower()
    assert "1080p" in response.text
    assert "4K (2160p)" in response.text
    assert "email" in response.text.lower()
    assert "delivered" in response.text.lower()
    assert (
        f"/search?q={quote_plus(release_title)}&amp;type=movie" in response.text
    )
    qbit = RecordingQbit()
    app.state.indexer = ScanIndexer([])
    app.state.qbit = qbit
    manual_search = client.get(
        f"/search?q={quote_plus(release_title)}&type=movie"
    )
    assert manual_search.status_code == 200
    assert qbit.add_calls == []
    with Session(app.state.engine) as database_session:
        assert database_session.exec(select(MediaJob)).all() == []


def test_event_read_action_updates_only_the_requested_event_and_404s(client, app):
    with Session(app.state.engine) as database_session:
        subscription = MediaSubscription(
            tmdb_id=603, type=MediaType.MOVIE, title="The Matrix"
        )
        database_session.add(subscription)
        database_session.commit()
        first = SubscriptionEvent(
            subscription_id=subscription.id,
            media_type=MediaType.MOVIE,
            kind=SubscriptionEventKind.RELEASE_MATCH,
            dedupe_key="release:event-read-first",
            title="First event",
            body="First event body",
        )
        second = SubscriptionEvent(
            subscription_id=subscription.id,
            media_type=MediaType.MOVIE,
            kind=SubscriptionEventKind.RELEASE_MATCH,
            dedupe_key="release:event-read-second",
            title="Second event",
            body="Second event body",
        )
        database_session.add_all([first, second])
        database_session.commit()
        first_id, second_id = first.id, second.id

    rejected = client.post(f"/events/{first_id}/read", follow_redirects=False)

    assert rejected.status_code == 403
    for csrf_tokens in (
        ("invalid-token", create_csrf_token(None)),
        (create_csrf_token(None), "invalid-token"),
    ):
        rejected_duplicate = client.post(
            f"/events/{first_id}/read",
            content=urlencode([("csrf_token", token) for token in csrf_tokens]),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

        assert rejected_duplicate.status_code == 403
        with Session(app.state.engine) as database_session:
            assert database_session.get(SubscriptionEvent, first_id).read_at is None
            assert database_session.get(SubscriptionEvent, second_id).read_at is None

    marked = client.post(
        f"/events/{first_id}/read",
        data={"csrf_token": create_csrf_token(None)},
        follow_redirects=False,
    )
    assert marked.headers["location"] == f"/events#event-{first_id}"
    assert client.post(
        "/events/999/read",
        data={"csrf_token": create_csrf_token(None)},
        follow_redirects=False,
    ).status_code == 404
    with Session(app.state.engine) as database_session:
        assert database_session.get(SubscriptionEvent, first_id).read_at is not None
        assert database_session.get(SubscriptionEvent, second_id).read_at is None


def test_events_routes_require_configured_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "events-auth.db"))
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    app = create_app()

    with TestClient(app) as client:
        history = client.get("/events", follow_redirects=False)
        read = client.post("/events/1/read", follow_redirects=False)

    assert history.status_code == 303
    assert history.headers["location"] == "/login?next=/events"
    assert read.status_code == 303
    assert read.headers["location"] == "/login?expired=1&next=/jobs"


def test_subscription_creation_is_idempotent_and_missing_media_is_404(client, app):
    matrix = TmdbMedia(603, MediaType.MOVIE, "The Matrix", "The Matrix", 1999, None)
    app.state.tmdb = FakeTmdb(media=matrix)

    assert client.post(
        "/subscriptions", data={"csrf_token": create_csrf_token(None), "tmdb_id": 603, "media_type": "movie"}, follow_redirects=False
    ).status_code == 303
    assert client.post(
        "/subscriptions", data={"csrf_token": create_csrf_token(None), "tmdb_id": 603, "media_type": "movie"}, follow_redirects=False
    ).status_code == 303
    with Session(app.state.engine) as database_session:
        assert len(database_session.exec(select(MediaSubscription)).all()) == 1

    app.state.tmdb = FakeTmdb(media=None)
    assert client.post(
        "/subscriptions", data={"csrf_token": create_csrf_token(None), "tmdb_id": 404, "media_type": "movie"}, follow_redirects=False
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

    assert client.post(f"/subscriptions/{subscription_id}/toggle", data={"csrf_token": create_csrf_token(None)}, follow_redirects=False).status_code == 303
    assert client.post(
        f"/subscriptions/{subscription_id}/releases/read", data={"csrf_token": create_csrf_token(None)}, follow_redirects=False
    ).status_code == 303
    with Session(app.state.engine) as database_session:
        enabled = database_session.get(MediaSubscription, subscription_id)
        release = database_session.exec(select(SubscriptionRelease)).one()
        assert enabled.is_active is True
        assert enabled.next_check_at is not None
        assert release.read_at is not None

    assert client.post(f"/subscriptions/{subscription_id}/delete", data={"csrf_token": create_csrf_token(None)}, follow_redirects=False).status_code == 303
    with Session(app.state.engine) as database_session:
        assert database_session.get(MediaSubscription, subscription_id) is None
        assert database_session.exec(select(SubscriptionRelease)).all() == []

    for action in ("toggle", "releases/read", "delete"):
        assert client.post(f"/subscriptions/999/{action}", data={"csrf_token": create_csrf_token(None)}, follow_redirects=False).status_code == 404


def test_delete_subscription_keeps_sourced_job_and_baseline_after_clearing_references(client, app):
    with Session(app.state.engine) as database_session:
        subscription = MediaSubscription(
            tmdb_id=603, type=MediaType.MOVIE, title="The Matrix", auto_download=True
        )
        database_session.add(subscription)
        database_session.commit()
        release = SubscriptionRelease(
            subscription_id=subscription.id,
            release_title="The.Matrix.1999.2160p",
            indexer="fake",
            size_bytes=1,
            seeders=9,
            leechers=0,
            download_url="magnet:?matrix",
            fingerprint="delete-sourced-release",
        )
        database_session.add(release)
        database_session.commit()
        subscription.auto_grabbed_release_id = release.id
        job = MediaJob(
            type=MediaType.MOVIE,
            title="The Matrix",
            year=1999,
            release_title=release.release_title,
            qbit_hash="matrix-hash",
            category="skald-movie",
            status=JobStatus.ORGANIZED,
            source_subscription_id=subscription.id,
            source_subscription_release_id=release.id,
        )
        database_session.add(job)
        database_session.commit()
        baseline = DownloadedQuality(
            media_type=MediaType.MOVIE,
            target_key="movie:tmdb:603",
            subscription_id=subscription.id,
            media_job_id=job.id,
            resolution="2160p",
            audio="atmos",
            hdr="dolby_vision",
            score_version="v1",
            quality_score=[4, 4, 5],
        )
        database_session.add(baseline)
        event = SubscriptionEvent(
            subscription_id=subscription.id,
            subscription_release_id=release.id,
            media_type=MediaType.MOVIE,
            kind=SubscriptionEventKind.RELEASE_MATCH,
            dedupe_key="release:delete-sourced-release",
            title="New movie release",
            body="The Matrix",
        )
        database_session.add(event)
        database_session.commit()
        database_session.add(NotificationDeliveryAttempt(
            event_id=event.id,
            channel=NotificationChannel.EMAIL,
        ))
        database_session.commit()
        subscription_id, release_id, job_id, baseline_id = (
            subscription.id,
            release.id,
            job.id,
            baseline.id,
        )

    assert client.post(f"/subscriptions/{subscription_id}/delete", data={"csrf_token": create_csrf_token(None)}, follow_redirects=False).status_code == 303

    with Session(app.state.engine) as database_session:
        assert database_session.get(MediaSubscription, subscription_id) is None
        assert database_session.get(SubscriptionRelease, release_id) is None
        job = database_session.get(MediaJob, job_id)
        baseline = database_session.get(DownloadedQuality, baseline_id)
        assert job is not None
        assert (job.source_subscription_id, job.source_subscription_release_id) == (None, None)
        assert baseline is not None
        assert (baseline.subscription_id, baseline.media_job_id) == (None, job_id)
        assert database_session.exec(select(SubscriptionEvent)).all() == []
        attempts = database_session.exec(select(NotificationDeliveryAttempt)).all()
        assert [(attempt.event_id, attempt.channel) for attempt in attempts] == [
            (None, NotificationChannel.EMAIL)
        ]


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
    context = captured_templates[-1][1]
    assert [(s["tmdb_id"], s["number"], s["name"]) for s in context["seasons"]] == [
        (3571, 0, "Specials"), (3572, 1, "Season 1"),
    ]
    assert context["seasons"][1]["episodes"] == [{"tmdb_id": 62001, "number": 1, "name": "Pilot"}]
    assert [s["loaded"] for s in context["seasons"]] == [False, True]
    assert context["seasons"][0]["episodes"] == []
    assert context["scopes"][0].season_number == 1
    assert context["series_scope_active"] is False
    assert context["selected_season_ids"] == {3572}
    assert context["selected_episode_ids"] == set()
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
    # Detail loads only the selected season; the JSON call is the second request.
    assert sorted(tmdb.season_requests) == [(1396, 1), (1396, 1)]


class SeasonsTmdb(FakeTmdb):
    EPISODES = {
        1: [TmdbEpisode(101, 1, "E1", None), TmdbEpisode(102, 2, "E2", None)],
        2: [TmdbEpisode(201, 1, "F1", None), TmdbEpisode(202, 2, "F2", None)],
    }

    def __init__(self, **kwargs):
        super().__init__(
            seasons=[TmdbSeason(10, 1, "Season 1", None, 2), TmdbSeason(20, 2, "Season 2", None, 2)],
            **kwargs,
        )

    async def get_tv_season(self, tmdb_id, season_number):
        if self.error:
            raise self.error
        season = self.seasons[season_number - 1]
        return TmdbTvSeason(
            season.tmdb_id, season_number, season.name, None, self.EPISODES[season_number]
        )


def _make_tv(app, with_scope=False):
    with Session(app.state.engine) as db:
        subscription = MediaSubscription(
            tmdb_id=1396, type=MediaType.TV, title="Show",
            next_check_at=datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=10_000),
        )
        db.add(subscription)
        db.commit()
        db.refresh(subscription)
        if with_scope:
            db.add(TvSubscriptionScope(
                subscription_id=subscription.id, tmdb_series_id=1396, includes_future_content=True
            ))
            db.commit()
        return subscription.id


def _scopes(app, subscription_id):
    with Session(app.state.engine) as db:
        return db.exec(
            select(TvSubscriptionScope).where(TvSubscriptionScope.subscription_id == subscription_id)
        ).all()


def _next_check(app, subscription_id):
    with Session(app.state.engine) as db:
        return db.get(MediaSubscription, subscription_id).next_check_at


def test_scope_series_mode_replaces_rows_and_bumps_next_check(client, app):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app)
    with Session(app.state.engine) as db:
        db.add(TvSubscriptionScope(
            subscription_id=subscription_id, tmdb_series_id=1396, tmdb_season_id=10, season_number=1
        ))
        db.commit()

    response = client.post(
        f"/subscriptions/{subscription_id}/scope", data={"csrf_token": create_csrf_token(None), "scope_mode": "series"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/subscriptions/{subscription_id}"
    rows = _scopes(app, subscription_id)
    assert len(rows) == 1
    assert rows[0].includes_future_content is True
    assert rows[0].tmdb_series_id == 1396
    assert rows[0].tmdb_season_id is None and rows[0].tmdb_episode_id is None
    next_check = _next_check(app, subscription_id)
    assert next_check.replace(tzinfo=UTC) <= datetime.now(UTC)


def test_scope_manual_mode_skips_episodes_covered_by_selected_season(client, app):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app, with_scope=True)

    response = client.post(
        f"/subscriptions/{subscription_id}/scope",
        data={"csrf_token": create_csrf_token(None), "scope_mode": "manual", "season_ids": [10], "episode_ids": [102, 202]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    rows = _scopes(app, subscription_id)
    shapes = sorted(
        (r.tmdb_season_id, r.season_number, r.tmdb_episode_id, r.episode_number,
         r.includes_future_content)
        for r in rows
    )
    assert shapes == [(10, 1, None, None, False), (20, 2, 202, 2, False)]
    assert _next_check(app, subscription_id).replace(tzinfo=UTC) <= datetime.now(UTC)


def test_scope_manual_empty_selection_clears_rows(client, app):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app, with_scope=True)

    response = client.post(
        f"/subscriptions/{subscription_id}/scope", data={"csrf_token": create_csrf_token(None), "scope_mode": "manual"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _scopes(app, subscription_id) == []


def test_scope_rejects_invalid_requests(client, app, captured_templates):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app, with_scope=True)
    with Session(app.state.engine) as db:
        movie = MediaSubscription(tmdb_id=603, type=MediaType.MOVIE, title="Matrix")
        db.add(movie)
        db.commit()
        db.refresh(movie)

    bad_season = client.post(
        f"/subscriptions/{subscription_id}/scope",
        data={"csrf_token": create_csrf_token(None), "scope_mode": "manual", "season_ids": [999]},
    )
    bad_episode = client.post(
        f"/subscriptions/{subscription_id}/scope",
        data={"csrf_token": create_csrf_token(None), "scope_mode": "manual", "episode_ids": [999]},
    )
    bad_mode = client.post(f"/subscriptions/{subscription_id}/scope", data={"csrf_token": create_csrf_token(None), "scope_mode": "nope"})
    not_tv = client.post(f"/subscriptions/{movie.id}/scope", data={"csrf_token": create_csrf_token(None), "scope_mode": "series"})
    app.state.tmdb = SeasonsTmdb(error=TmdbError("boom"))
    failed = client.post(f"/subscriptions/{subscription_id}/scope", data={"csrf_token": create_csrf_token(None), "scope_mode": "manual"})

    assert (bad_season.status_code, bad_episode.status_code) == (400, 400)
    assert bad_mode.status_code == 400
    assert not_tv.status_code == 404
    assert failed.status_code == 502
    assert len(_scopes(app, subscription_id)) == 1


def test_detail_context_exposes_selection_and_episodes(client, app, captured_templates):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app, with_scope=True)
    with Session(app.state.engine) as db:
        db.add(TvSubscriptionScope(
            subscription_id=subscription_id, tmdb_series_id=1396, tmdb_season_id=20,
            season_number=2, tmdb_episode_id=202, episode_number=2,
        ))
        db.add(TvSubscriptionScope(
            subscription_id=subscription_id, tmdb_series_id=1396, tmdb_season_id=10, season_number=1
        ))
        db.commit()

    assert client.get(f"/subscriptions/{subscription_id}").status_code == 200

    context = captured_templates[-1][1]
    assert context["series_scope_active"] is True
    assert context["selected_season_ids"] == {10}
    assert context["selected_episode_ids"] == {202}
    assert [e["tmdb_id"] for e in context["seasons"][1]["episodes"]] == [201, 202]


def test_auto_download_redirects_to_detail_from_referer_and_bumps_next_check(client, app):
    subscription_id = _make_tv(app)

    from_detail = client.post(
        f"/subscriptions/{subscription_id}/auto-download",
        data={"csrf_token": create_csrf_token(None)},
        headers={"referer": f"http://testserver/subscriptions/{subscription_id}"},
        follow_redirects=False,
    )
    assert from_detail.headers["location"] == f"/subscriptions/{subscription_id}"
    assert _next_check(app, subscription_id).replace(tzinfo=UTC) <= datetime.now(UTC)

    other = client.post(
        f"/subscriptions/{subscription_id}/auto-download",
        data={"csrf_token": create_csrf_token(None)},
        headers={"referer": "http://evil.example/elsewhere"},
        follow_redirects=False,
    )
    assert other.headers["location"] == "/subscriptions"


async def test_series_scope_lets_scan_store_tv_release(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title="Химкинские ведьмы", next_check_at=now
    )
    session.add(subscription)
    session.commit()
    session.add(TvSubscriptionScope(
        subscription_id=subscription.id, tmdb_series_id=1, includes_future_content=True
    ))
    session.commit()

    await scan_due_subscriptions(
        session,
        ScanIndexer([ReleaseResult(
            "Химкинские ведьмы / S1E1-17 of 17 [2025, WEBRip 1080p]", "fake", 1, 5, 0, "magnet:?x"
        )]),
        interval_seconds=60,
        now=now,
    )

    releases = session.exec(select(SubscriptionRelease)).all()
    assert [r.release_title for r in releases] == [
        "Химкинские ведьмы / S1E1-17 of 17 [2025, WEBRip 1080p]"
    ]


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
    assert failed.status_code == 200
    assert captured_templates[-1][0] == "subscription_detail.html"
    assert captured_templates[-1][1]["seasons_error"] == "TMDB request failed"
    assert captured_templates[-1][1]["seasons"] == []
    season = client.get(f"/subscriptions/{tv.id}/seasons/1")
    assert season.status_code == 502
    assert season.headers["content-type"].startswith("application/json")
    assert season.json() == {"error": "TMDB request failed"}
    assert client.get("/subscriptions/999/seasons/1").json()["error"]
    assert client.get(f"/subscriptions/{movie.id}/seasons/1").status_code == 404


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


def test_tv_release_search_link_keeps_tv_type_and_forms_carry_csrf(client, app):
    subscription_id = _make_tv(app)
    with Session(app.state.engine) as db:
        db.add(SubscriptionRelease(
            subscription_id=subscription_id, release_title="Show.S01E01", indexer="fake",
            size_bytes=1, seeders=1, leechers=0, download_url="magnet:?tvx", fingerprint="tv-x",
        ))
        db.commit()

    page = client.get("/subscriptions")
    assert "type=tv" in page.text
    assert "Setup required" in page.text
    assert 'data-confirm="Delete' in page.text
    assert page.text.count(f'name="csrf_token" value="{create_csrf_token(None)}"') >= 3
    filtered = client.get(f"/subscriptions?unread=1&subscription={subscription_id}")
    assert "Show.S01E01" in filtered.text


def test_post_without_csrf_is_rejected_and_return_to_is_validated(client, app):
    subscription_id = _make_tv(app)
    assert client.post(f"/subscriptions/{subscription_id}/toggle").status_code == 403

    token = {"csrf_token": create_csrf_token(None)}
    ok = client.post(
        f"/subscriptions/{subscription_id}/toggle",
        data={**token, "return_to": f"/subscriptions/{subscription_id}"},
        follow_redirects=False,
    )
    assert ok.headers["location"] == f"/subscriptions/{subscription_id}"
    evil = client.post(
        f"/subscriptions/{subscription_id}/toggle",
        data={**token, "return_to": "//evil.example"},
        follow_redirects=False,
    )
    assert evil.headers["location"] == "/subscriptions"


def test_detail_renders_without_scope_and_on_tmdb_error(client, app):
    subscription_id = _make_tv(app)
    app.state.tmdb = SeasonsTmdb(error=TmdbError("boom"))
    page = client.get(f"/subscriptions/{subscription_id}")
    assert page.status_code == 200
    assert "Setup required" in page.text
    assert "Not configured" in page.text
    assert "Retry" in page.text


# ---------------------------------------------------------------------------
# Lazy seasons, preserved selections
# ---------------------------------------------------------------------------


def test_detail_lazy_loads_only_selected_seasons_and_season_query(client, app, captured_templates):
    tmdb = SeasonsTmdb()
    app.state.tmdb = tmdb
    subscription_id = _make_tv(app)

    assert client.get(f"/subscriptions/{subscription_id}").status_code == 200
    assert tmdb.season_requests == []  # SeasonsTmdb overrides get_tv_season; check context instead
    context = captured_templates[-1][1]
    assert [s["loaded"] for s in context["seasons"]] == [False, False]
    assert [s["total"] for s in context["seasons"]] == [2, 2]

    html = client.get(f"/subscriptions/{subscription_id}?season=2")
    assert html.status_code == 200
    context = captured_templates[-1][1]
    assert [s["loaded"] for s in context["seasons"]] == [False, True]


def test_scope_save_preserves_episode_picks_of_unloaded_seasons(client, app):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app)
    with Session(app.state.engine) as db:
        db.add(TvSubscriptionScope(
            subscription_id=subscription_id, tmdb_series_id=1396, tmdb_season_id=20,
            season_number=2, tmdb_episode_id=202, episode_number=2,
        ))
        db.add(TvSubscriptionScope(
            subscription_id=subscription_id, tmdb_series_id=1396, tmdb_season_id=10,
            season_number=1, tmdb_episode_id=101, episode_number=1,
        ))
        db.commit()

    # Season 1 was loaded (and its pick cleared in favour of E2); season 2 was not.
    response = client.post(
        f"/subscriptions/{subscription_id}/scope",
        data={
            "csrf_token": create_csrf_token(None), "scope_mode": "manual", "lazy": 1,
            "loaded_season_ids": [10], "episode_ids": [102],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    shapes = sorted(
        (r.tmdb_season_id, r.tmdb_episode_id) for r in _scopes(app, subscription_id)
    )
    assert shapes == [(10, 102), (20, 202)]


def test_scope_save_whole_season_supersedes_unloaded_episode_picks(client, app):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app)
    with Session(app.state.engine) as db:
        db.add(TvSubscriptionScope(
            subscription_id=subscription_id, tmdb_series_id=1396, tmdb_season_id=20,
            season_number=2, tmdb_episode_id=202, episode_number=2,
        ))
        db.commit()

    client.post(
        f"/subscriptions/{subscription_id}/scope",
        data={"csrf_token": create_csrf_token(None), "scope_mode": "manual", "lazy": 1,
              "season_ids": [20]},
        follow_redirects=False,
    )

    assert [(r.tmdb_season_id, r.tmdb_episode_id) for r in _scopes(app, subscription_id)] == [
        (20, None)
    ]


def test_scope_save_rejects_episode_from_unloaded_season_in_lazy_mode(client, app):
    app.state.tmdb = SeasonsTmdb()
    subscription_id = _make_tv(app)

    response = client.post(
        f"/subscriptions/{subscription_id}/scope",
        data={"csrf_token": create_csrf_token(None), "scope_mode": "manual", "lazy": 1,
              "loaded_season_ids": [10], "episode_ids": [202]},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Baseline coverage, direct grab, flash
# ---------------------------------------------------------------------------


async def test_tv_auto_download_skips_episode_with_baseline_without_subscription_job(session):
    """A baseline counts as covered even when no job row is linked to the subscription."""
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _tv_series_subscription(session, now)
    job = MediaJob(
        type=MediaType.TV, title="Show", season=1, episode=2, episode_set="[2]",
        release_title="Show.S01E02.1080p.WEB", qbit_hash="h", category="tv",
        status=JobStatus.ORGANIZED,  # no source_subscription_id: link is gone
    )
    session.add(job)
    session.commit()
    session.add(DownloadedQuality(
        media_type=MediaType.TV,
        target_key=f"tv:tmdb:{subscription.tmdb_id}:season:1:episode:2",
        subscription_id=subscription.id,
        media_job_id=job.id,
        resolution="1080p", audio="", hdr="", score_version="v1", quality_score=[1],
    ))
    session.commit()
    qbit = SelectiveRecordingQbit(_TV_FILES)

    await _tv_scan(session, qbit, [
        ReleaseResult("Show.S01E02.2160p.WEB", "fake", 1, 5, 0, "magnet:?again"),
        ReleaseResult("Show.S01E03.1080p.WEB", "fake", 1, 5, 0, "magnet:?next"),
    ], now)

    assert qbit.paused_add_calls == [("magnet:?next", "tv")]


def _seed_release(app, subscription_type, **scope_kwargs):
    with Session(app.state.engine) as db:
        subscription = MediaSubscription(
            tmdb_id=1396, type=subscription_type, title="Show", year=2020,
            next_check_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        db.add(subscription)
        db.commit()
        db.refresh(subscription)
        title = "Show.2020.1080p.WEB" if subscription_type is MediaType.MOVIE else "Show.S01E02-E03.1080p.WEB"
        release = SubscriptionRelease(
            subscription_id=subscription.id, release_title=title, indexer="fake",
            size_bytes=1, seeders=1, leechers=0, download_url="magnet:?xt=urn:btih:" + "a" * 40,
            fingerprint=f"fp-{subscription_type.value}",
        )
        db.add(release)
        db.commit()
        db.refresh(release)
        if scope_kwargs:
            scope = TvSubscriptionScope(
                subscription_id=subscription.id, tmdb_series_id=1396, **scope_kwargs
            )
            db.add(scope)
            db.commit()
            db.refresh(scope)
            db.add(SubscriptionReleaseScope(
                subscription_release_id=release.id, tv_subscription_scope_id=scope.id
            ))
            db.commit()
        return subscription.id, release.id


def _grab(client, release_id, **kwargs):
    return client.post(
        f"/subscriptions/releases/{release_id}/grab",
        data={"csrf_token": create_csrf_token(None)},
        follow_redirects=False,
        **kwargs,
    )


def test_release_grab_movie_creates_job_with_source_ids_and_marks_read(client, app):
    qbit = RecordingQbit()
    app.state.qbit = qbit
    subscription_id, release_id = _seed_release(app, MediaType.MOVIE)

    response = _grab(client, release_id)

    assert response.status_code == 303
    with Session(app.state.engine) as db:
        job = db.exec(select(MediaJob)).one()
        assert response.headers["location"] == f"/jobs/{job.id}"
        assert (job.type, job.title, job.year) == (MediaType.MOVIE, "Show", 2020)
        assert (job.source_subscription_id, job.source_subscription_release_id) == (
            subscription_id, release_id
        )
        assert db.get(SubscriptionRelease, release_id).read_at is not None
    assert len(qbit.add_calls) == 1
    assert "flash=" in response.headers["set-cookie"]


def test_release_grab_tv_targets_scoped_episodes_only(client, app):
    qbit = SelectiveRecordingQbit(_TV_FILES)
    app.state.qbit = qbit
    _, release_id = _seed_release(
        app, MediaType.TV, tmdb_season_id=10, season_number=1, tmdb_episode_id=102, episode_number=3
    )

    response = _grab(client, release_id)

    assert response.status_code == 303
    with Session(app.state.engine) as db:
        job = db.exec(select(MediaJob)).one()
        assert (job.season, job.episode, job.episode_set) == (1, 3, "[3]")
        assert job.source_subscription_release_id == release_id
    assert [call for call in qbit.priority_calls if call[2] == 1][-1][1] == [3]


def test_release_grab_unaddressable_tv_redirects_to_search_with_warning(client, app):
    qbit = SelectiveRecordingQbit(_TV_FILES)
    app.state.qbit = qbit
    _, release_id = _seed_release(app, MediaType.TV)  # no persisted scopes

    response = _grab(client, release_id)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/search?q=Show.S01E02-E03.1080p.WEB&type=tv")
    assert "flash=" in response.headers["set-cookie"]
    with Session(app.state.engine) as db:
        assert db.exec(select(MediaJob)).all() == []


def test_release_grab_requires_csrf_and_404s(client, app):
    app.state.qbit = RecordingQbit()
    _, release_id = _seed_release(app, MediaType.MOVIE)

    assert client.post(f"/subscriptions/releases/{release_id}/grab").status_code == 403
    assert _grab(client, 9999).status_code == 404
    with Session(app.state.engine) as db:
        assert db.exec(select(MediaJob)).all() == []


def test_release_grab_dedupes_active_job(client, app):
    class HashQbit(RecordingQbit):
        def add_torrent(self, download_url, category):
            super().add_torrent(download_url, category)
            return "a" * 40

    qbit = HashQbit()
    app.state.qbit = qbit
    _, release_id = _seed_release(app, MediaType.MOVIE)

    first = _grab(client, release_id)
    second = _grab(client, release_id)

    assert first.headers["location"] == second.headers["location"]
    assert len(qbit.add_calls) == 1
    with Session(app.state.engine) as db:
        assert len(db.exec(select(MediaJob)).all()) == 1


def test_release_grab_qbit_failure_renders_error_page(client, app, captured_templates):
    app.state.qbit = RecordingQbit(failures=1)
    _, release_id = _seed_release(app, MediaType.MOVIE)

    response = _grab(client, release_id)

    assert response.status_code == 502
    assert captured_templates[-1][0] == "error.html"
    assert captured_templates[-1][1]["back_url"] == "/subscriptions#releases"


def test_subscription_actions_set_flash_messages(client, app):
    subscription_id = _make_tv(app)
    token = {"csrf_token": create_csrf_token(None)}

    paused = client.post(f"/subscriptions/{subscription_id}/toggle", data=token, follow_redirects=False)
    auto = client.post(f"/subscriptions/{subscription_id}/auto-download", data=token, follow_redirects=False)
    saved = client.post(
        f"/subscriptions/{subscription_id}/scope", data={**token, "scope_mode": "series"},
        follow_redirects=False,
    )
    deleted = client.post(f"/subscriptions/{subscription_id}/delete", data=token, follow_redirects=False)

    for response in (paused, auto, saved, deleted):
        assert "flash=" in response.headers["set-cookie"]


async def test_forgotten_organized_tv_job_still_blocks_auto_regrab(tmp_path, monkeypatch):
    """Hiding a job via /forget keeps its row, so auto-grab coverage still sees it."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "forget-regrab.db"))
    app = create_app()
    now = datetime(2026, 9, 3, tzinfo=UTC)

    class ForgetQbit:
        def __init__(self):
            self.deleted = []

        def delete_torrent(self, torrent_hash, delete_files=True):
            self.deleted.append((torrent_hash, delete_files))

    with TestClient(app) as client:
        app.state.qbit = ForgetQbit()
        with Session(app.state.engine) as session:
            subscription = _tv_series_subscription(session, now)
            job = MediaJob(
                type=MediaType.TV, title="Show", season=1, episode=2,
                release_title="Show.S01E02.1080p.WEB", qbit_hash="old-hash",
                category="tv", status=JobStatus.ORGANIZED,
                source_subscription_id=subscription.id,
            )
            session.add(job)
            session.commit()
            job_id = job.id

        response = client.post(
            f"/jobs/{job_id}/forget",
            data={"csrf_token": create_csrf_token(None)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert app.state.qbit.deleted == [("old-hash", False)]

    qbit = SelectiveRecordingQbit(_TV_FILES)
    with Session(app.state.engine) as session:
        assert session.get(MediaJob, job_id).hidden_at is not None

        await _tv_scan(session, qbit, [
            ReleaseResult("Show.S01E02.2160p.WEB", "fake", 1, 5, 0, "magnet:?e2new"),
        ], now)
        assert len(session.exec(select(MediaJob)).all()) == 1
        assert qbit.paused_add_calls == []

        await _tv_scan(session, qbit, [
            ReleaseResult("Show.S01E02.2160p.WEB", "fake", 1, 5, 0, "magnet:?e2new"),
            ReleaseResult("Show.S01E03.1080p.WEB", "fake", 1, 5, 0, "magnet:?e3"),
        ], now + timedelta(seconds=60))
        jobs = session.exec(select(MediaJob).where(MediaJob.id != job_id)).all()
        assert [(j.season, j.episode_set) for j in jobs] == [(1, "[3]")]
        assert qbit.paused_add_calls == [("magnet:?e3", "tv")]
