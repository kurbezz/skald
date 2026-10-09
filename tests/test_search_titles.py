from datetime import UTC, datetime

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlmodel import Session, select

from skald.auth import create_csrf_token
from skald.db import get_engine, migrate_schema
from skald.indexer.base import ReleaseResult
from skald.main import create_app
from skald.models import (
    MediaSubscription,
    MediaType,
    SubscriptionEvent,
    SubscriptionRelease,
    TvSubscriptionScope,
)
from skald.subscriptions import (
    release_matches_subscription,
    scan_due_subscriptions,
    subscription_queries,
)
from skald.tmdb import TmdbClient, TmdbError, TmdbMedia, build_search_titles

from tests.test_subscriptions import FakeTmdb, session  # noqa: F401 - fixture reuse

TMDB_URL = "https://api.themoviedb.org/3"
NOW = datetime(2026, 9, 3, tzinfo=UTC)


def tv(titles, **kwargs):
    return MediaSubscription(
        tmdb_id=1, type=MediaType.TV, title=titles[-1], search_titles=titles, **kwargs
    )


# --- TMDB -------------------------------------------------------------------


@respx.mock
async def test_get_localized_title_requests_language():
    route = respx.get(f"{TMDB_URL}/tv/124364").mock(
        return_value=Response(200, json={"id": 124364, "name": "Извне"})
    )
    client = TmdbClient("token")
    title = await client.get_localized_title(124364, MediaType.TV, "ru-RU")
    await client.aclose()

    assert title == "Извне"
    assert route.calls[0].request.url.params["language"] == "ru-RU"


@respx.mock
async def test_get_localized_title_movie_uses_title_field_and_errors_are_tmdb_errors():
    respx.get(f"{TMDB_URL}/movie/1").mock(return_value=Response(200, json={"title": "Матрица"}))
    respx.get(f"{TMDB_URL}/movie/2").mock(return_value=Response(404))
    client = TmdbClient("token")

    assert await client.get_localized_title(1, MediaType.MOVIE, "ru-RU") == "Матрица"
    with pytest.raises(TmdbError):
        await client.get_localized_title(2, MediaType.MOVIE, "ru-RU")
    await client.aclose()


def test_build_search_titles_is_unique_and_ordered():
    assert build_search_titles(["Извне"], "FROM", "From") == ["Извне", "FROM"]
    assert build_search_titles([], None, "Show") == ["Show"]


# --- migration --------------------------------------------------------------


def test_migration_adds_search_titles_column_idempotently(tmp_path):
    engine = get_engine(str(tmp_path / "old.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE mediasubscription (id INTEGER PRIMARY KEY, tmdb_id INTEGER NOT NULL, "
            "type VARCHAR NOT NULL, title VARCHAR NOT NULL, original_title VARCHAR, year INTEGER, "
            "poster_url VARCHAR, is_active BOOLEAN NOT NULL, auto_download BOOLEAN NOT NULL DEFAULT 0, "
            "auto_grabbed_release_id INTEGER, created_at DATETIME NOT NULL, "
            "last_checked_at DATETIME, next_check_at DATETIME NOT NULL, last_error VARCHAR)"
        )
        connection.exec_driver_sql(
            "INSERT INTO mediasubscription (id, tmdb_id, type, title, is_active, created_at, "
            "next_check_at) VALUES (1, 5, 'TV', 'Old', 1, '2026-01-01', '2026-01-01')"
        )

    migrate_schema(engine)
    migrate_schema(engine)

    with Session(engine) as session:
        columns = {c[1] for c in session.connection().exec_driver_sql(
            "PRAGMA table_info(mediasubscription)"
        ).fetchall()}
        subscription = session.exec(select(MediaSubscription)).one()
    assert "search_titles" in columns
    assert subscription.search_titles is None


# --- creation ---------------------------------------------------------------


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "titles.db"))
    return create_app()


def _create(app, tmdb):
    app.state.tmdb = tmdb
    with TestClient(app) as client:
        app.state.tmdb = tmdb
        return client.post(
            "/subscriptions",
            data={"csrf_token": create_csrf_token(None), "tmdb_id": 124364, "media_type": "tv"},
            follow_redirects=False,
        )


def test_creation_stores_localized_search_titles(app):
    show = TmdbMedia(124364, MediaType.TV, "From", "FROM", 2022, None)
    tmdb = FakeTmdb(media=show)
    tmdb.localized = {"ru-RU": "Извне"}

    assert _create(app, tmdb).status_code == 303

    with Session(app.state.engine) as db:
        assert db.exec(select(MediaSubscription)).one().search_titles == ["Извне", "FROM"]


def test_creation_survives_localized_title_failure(app):
    show = TmdbMedia(124364, MediaType.TV, "From", "FROM", 2022, None)
    tmdb = FakeTmdb(media=show)
    tmdb.localized_error = TmdbError("boom")

    assert _create(app, tmdb).status_code == 303

    with Session(app.state.engine) as db:
        assert db.exec(select(MediaSubscription)).one().search_titles == ["FROM"]


# --- queries ----------------------------------------------------------------


def test_tv_queries_have_no_year_and_use_each_title_movie_keeps_year():
    show = tv(["Извне", "FROM"], year=2022)
    movie = MediaSubscription(
        tmdb_id=2, type=MediaType.MOVIE, title="Matrix", year=1999, search_titles=["Матрица", "Matrix"]
    )

    assert subscription_queries(show) == ["Извне", "FROM"]
    assert subscription_queries(movie) == ["Матрица 1999", "Matrix 1999"]
    assert subscription_queries(
        MediaSubscription(tmdb_id=3, type=MediaType.TV, title="T", original_title="O")
    ) == ["O", "T"]


# --- matching ---------------------------------------------------------------

FROM_RELEASE = (
    "Извне / From / S4E1-10 of 10 (Джек Бендер) [2026, США, фантастика, WEB-DL 1080p] "
    "MVO (LostFilm)"
)


@pytest.mark.parametrize(
    ("titles", "release", "expected"),
    [
        (["Извне", "FROM"], FROM_RELEASE, True),
        (["FROM"], FROM_RELEASE, True),
        (["Извне", "FROM"],
         "(Score) [CD] Кобра Кай / Cobra Kai: Season IV (4) (Soundtrack From The Netflix "
         "Original Series) [2022, FLAC]", False),
        (["Извне", "FROM"],
         "Синистер. Из тьмы / Ur mörkret / From Darkness (Мартин Монрад) [2018, WEB-DL 1080p]",
         False),
        (["Химкинские ведьмы", "Khimki Witches"],
         "Химкинские ведьмы / S1E1-17 of 17 (Иван Иванов) [2025, Россия, WEBRip 1080p]", True),
        (["Эпидемия", "Epidemic"], "Эпидемия / Epidemiya / S01E01-08 [2019, WEB-DL 1080p]", True),
        (["Ёжик"], "Ежик / S01E01 [2020, WEB-DL]", True),
    ],
)
def test_release_title_matching(titles, release, expected):
    assert release_matches_subscription(tv(titles), release) is expected


def test_matching_strips_trailing_season_fragment():
    subscription = MediaSubscription(
        tmdb_id=1, type=MediaType.MOVIE, title="Show", search_titles=["Show"]
    )
    assert release_matches_subscription(subscription, "Show: Season IV (4) [2022]") is True


# --- scanning ---------------------------------------------------------------


class QueryIndexer:
    def __init__(self, by_query, failing=()):
        self.by_query = by_query
        self.failing = set(failing)
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        if query in self.failing:
            raise RuntimeError("indexer down")
        return self.by_query.get(query, [])


def _scoped_tv(session, titles=None, **kwargs):
    subscription = MediaSubscription(
        tmdb_id=124364, type=MediaType.TV, title="From", original_title="FROM", year=2022,
        search_titles=titles, next_check_at=NOW, **kwargs,
    )
    session.add(subscription)
    session.commit()
    session.add(TvSubscriptionScope(
        subscription_id=subscription.id, tmdb_series_id=124364, includes_future_content=True
    ))
    session.commit()
    return subscription


async def test_scan_searches_each_title_without_year_and_skips_foreign_releases(session):
    subscription = _scoped_tv(session, ["Извне", "FROM"])
    good = ReleaseResult(FROM_RELEASE, "fake", 1, 50, 0, "magnet:?a", guid="a")
    cobra = ReleaseResult(
        "(Score) [CD] Кобра Кай / Cobra Kai: Season IV (4) (Soundtrack From The Netflix "
        "Original Series) [2022, FLAC] S4", "fake", 1, 50, 0, "magnet:?b", guid="b",
    )
    indexer = QueryIndexer({"Извне": [good, cobra], "FROM": [good]})

    await scan_due_subscriptions(session, indexer, interval_seconds=60, now=NOW)

    assert indexer.queries == ["Извне", "FROM"]
    assert [r.release_title for r in session.exec(select(SubscriptionRelease)).all()] == [
        FROM_RELEASE
    ]
    assert len(session.exec(select(SubscriptionEvent)).all()) == 1
    session.refresh(subscription)
    assert subscription.last_error is None


async def test_scan_tolerates_partial_query_failure_but_not_total_failure(session):
    subscription = _scoped_tv(session, ["Извне", "FROM"])
    good = ReleaseResult(FROM_RELEASE, "fake", 1, 50, 0, "magnet:?a", guid="a")

    await scan_due_subscriptions(
        session, QueryIndexer({"FROM": [good]}, failing={"Извне"}), interval_seconds=60, now=NOW
    )
    session.refresh(subscription)
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1
    assert subscription.last_error is None

    subscription.next_check_at = NOW
    session.add(subscription)
    session.commit()
    await scan_due_subscriptions(
        session, QueryIndexer({}, failing={"Извне", "FROM"}), interval_seconds=60, now=NOW
    )
    session.refresh(subscription)
    assert subscription.last_error


async def test_scan_lazily_backfills_search_titles(session):
    subscription = _scoped_tv(session, None)
    tmdb = FakeTmdb()
    tmdb.localized = {"ru-RU": "Извне"}
    indexer = QueryIndexer({})

    await scan_due_subscriptions(session, indexer, tmdb=tmdb, interval_seconds=60, now=NOW)

    session.refresh(subscription)
    assert subscription.search_titles == ["Извне", "FROM"]
    assert indexer.queries == ["Извне", "FROM"]


async def test_scan_backfill_failure_falls_back_without_persisting(session):
    subscription = _scoped_tv(session, None)
    tmdb = FakeTmdb()
    tmdb.localized_error = TmdbError("down")
    indexer = QueryIndexer({})

    await scan_due_subscriptions(session, indexer, tmdb=tmdb, interval_seconds=60, now=NOW)

    session.refresh(subscription)
    assert subscription.search_titles is None
    assert indexer.queries == ["FROM"]
