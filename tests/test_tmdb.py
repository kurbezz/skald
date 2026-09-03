import asyncio

import httpx
import pytest
import respx
from httpx import Response

from skald.models import MediaType
from skald.tmdb import TmdbClient, TmdbError


TMDB_URL = "https://api.themoviedb.org/3"


def configuration_response(*, sizes: list[str] | None = None) -> Response:
    return Response(
        200,
        json={
            "images": {
                "secure_base_url": "https://images.example/",
                "poster_sizes": sizes or ["w342"],
            }
        },
    )


@respx.mock
async def test_search_normalizes_movie_and_tv():
    configuration = respx.get(f"{TMDB_URL}/configuration").mock(
        return_value=configuration_response()
    )
    route = respx.get(f"{TMDB_URL}/search/multi").mock(
        return_value=Response(200, json={"results": [
            {
                "id": 603,
                "media_type": "movie",
                "title": "The Matrix",
                "original_title": "The Matrix",
                "release_date": "1999-03-30",
                "poster_path": "/matrix.jpg",
            },
            {
                "id": 1396,
                "media_type": "tv",
                "name": "Breaking Bad",
                "original_name": "Breaking Bad",
                "first_air_date": "2008-01-20",
                "poster_path": None,
            },
            {"id": 1, "media_type": "person", "name": "Keanu Reeves"},
        ]})
    )

    client = TmdbClient("token")
    results = await client.search("matrix")
    await client.aclose()

    assert route.called
    assert configuration.called
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer token"
    assert dict(request.url.params) == {
        "query": "matrix",
        "language": "en-US",
        "include_adult": "false",
    }
    assert [(item.tmdb_id, item.type, item.year, item.poster_url) for item in results] == [
        (603, MediaType.MOVIE, 1999, "https://images.example/w342/matrix.jpg"),
        (1396, MediaType.TV, 2008, None),
    ]


@respx.mock
async def test_search_handles_missing_dates_posters_and_unsupported_types():
    respx.get(f"{TMDB_URL}/search/multi").mock(
        return_value=Response(200, json={"results": [
            {"id": 1, "media_type": "movie", "title": "No Date", "release_date": "", "poster_path": None},
            {"id": 2, "media_type": "tv", "name": "No Poster", "first_air_date": None},
            {"id": 3, "media_type": "person", "name": "Ignored"},
            {"media_type": "movie", "title": "No ID"},
            {"id": 4, "media_type": "movie", "title": ""},
        ]})
    )

    client = TmdbClient("token")
    results = await client.search("query")
    await client.aclose()

    assert [(item.tmdb_id, item.title, item.year, item.poster_url) for item in results] == [
        (1, "No Date", None, None),
        (2, "No Poster", None, None),
    ]


@respx.mock
async def test_get_media_normalizes_movie():
    route = respx.get(f"{TMDB_URL}/movie/603").mock(
        return_value=Response(200, json={
            "id": 603,
            "title": "The Matrix",
            "original_title": "The Matrix",
            "release_date": "1999-03-30",
            "poster_path": None,
        })
    )

    client = TmdbClient("token")
    media = await client.get_media(603, MediaType.MOVIE)
    await client.aclose()

    assert route.called
    assert (media.tmdb_id, media.type, media.title, media.original_title, media.year, media.poster_url) == (
        603,
        MediaType.MOVIE,
        "The Matrix",
        "The Matrix",
        1999,
        None,
    )


@respx.mock
async def test_get_tv_seasons_normalizes_specials_and_regular_seasons():
    route = respx.get(f"{TMDB_URL}/tv/1396").mock(
        return_value=Response(200, json={"seasons": [
            {"id": 3571, "season_number": 0, "name": "Specials", "air_date": "2009-02-17", "episode_count": 10},
            {"id": 3572, "season_number": 1, "name": "Season 1", "air_date": "2008-01-20", "episode_count": 7},
            {"id": "invalid", "season_number": 2, "name": "Ignored", "episode_count": 13},
        ]})
    )

    client = TmdbClient("token")
    seasons = await client.get_tv_seasons(1396)
    await client.aclose()

    assert route.called
    assert dict(route.calls[0].request.url.params) == {"language": "en-US"}
    assert [(season.tmdb_id, season.season_number, season.name, season.air_date, season.episode_count) for season in seasons] == [
        (3571, 0, "Specials", "2009-02-17", 10),
        (3572, 1, "Season 1", "2008-01-20", 7),
    ]


@respx.mock
async def test_get_tv_season_normalizes_episodes():
    route = respx.get(f"{TMDB_URL}/tv/1396/season/2").mock(
        return_value=Response(200, json={
            "id": 3573,
            "season_number": 2,
            "name": "Season 2",
            "air_date": "2009-03-08",
            "episodes": [
                {"id": 62082, "episode_number": 1, "name": "Seven Thirty-Seven", "air_date": "2009-03-08"},
                {"id": 62083, "episode_number": 2, "name": "Grilled", "air_date": None},
                {"id": 0, "episode_number": 3, "name": "Ignored"},
            ],
        })
    )

    client = TmdbClient("token")
    season = await client.get_tv_season(1396, 2)
    await client.aclose()

    assert route.called
    assert dict(route.calls[0].request.url.params) == {"language": "en-US"}
    assert (season.tmdb_id, season.season_number, season.name, season.air_date) == (
        3573, 2, "Season 2", "2009-03-08"
    )
    assert [(episode.tmdb_id, episode.episode_number, episode.name, episode.air_date) for episode in season.episodes] == [
        (62082, 1, "Seven Thirty-Seven", "2009-03-08"),
        (62083, 2, "Grilled", None),
    ]


@respx.mock
async def test_configuration_is_cached_and_requires_w342():
    configuration = respx.get(f"{TMDB_URL}/configuration").mock(
        return_value=configuration_response()
    )
    respx.get(f"{TMDB_URL}/search/multi").mock(
        return_value=Response(200, json={"results": [
            {"id": 1, "media_type": "movie", "title": "One", "poster_path": "/one.jpg"},
            {"id": 2, "media_type": "tv", "name": "Two", "poster_path": "/two.jpg"},
        ]})
    )

    client = TmdbClient("token")
    results = await client.search("two posters")
    await client.aclose()

    assert configuration.call_count == 1
    assert [item.poster_url for item in results] == [
        "https://images.example/w342/one.jpg",
        "https://images.example/w342/two.jpg",
    ]


@respx.mock
async def test_unsupported_poster_size_does_not_build_poster_url():
    respx.get(f"{TMDB_URL}/configuration").mock(
        return_value=configuration_response(sizes=["w185"])
    )
    respx.get(f"{TMDB_URL}/search/multi").mock(
        return_value=Response(200, json={"results": [
            {"id": 603, "media_type": "movie", "title": "The Matrix", "poster_path": "/matrix.jpg"},
        ]})
    )

    client = TmdbClient("token")
    results = await client.search("matrix")
    await client.aclose()

    assert results[0].poster_url is None


@respx.mock
async def test_search_retries_rate_limit_then_succeeds():
    route = respx.get(f"{TMDB_URL}/search/multi").mock(
        side_effect=[
            Response(429),
            Response(200, json={"results": [
                {"id": 603, "media_type": "movie", "title": "The Matrix"},
            ]}),
        ]
    )

    client = TmdbClient("secret-token")
    results = await client.search("matrix")
    await client.aclose()

    assert route.call_count == 2
    assert [(item.tmdb_id, item.title) for item in results] == [(603, "The Matrix")]


async def test_search_retries_connect_error_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        return Response(200, json={"results": [
            {"id": 603, "media_type": "movie", "title": "The Matrix"},
        ]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url=TMDB_URL, transport=transport) as http_client:
        client = TmdbClient("secret-token", client=http_client)
        results = await client.search("matrix")

    assert calls == 2
    assert [(item.tmdb_id, item.title) for item in results] == [(603, "The Matrix")]


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
@respx.mock
async def test_search_retries_transient_server_error_then_succeeds(status_code):
    route = respx.get(f"{TMDB_URL}/search/multi").mock(
        side_effect=[
            Response(status_code),
            Response(200, json={"results": [
                {"id": 603, "media_type": "movie", "title": "The Matrix"},
            ]}),
        ]
    )

    client = TmdbClient("secret-token")
    results = await client.search("matrix")
    await client.aclose()

    assert route.call_count == 2
    assert [(item.tmdb_id, item.title) for item in results] == [(603, "The Matrix")]


@respx.mock
async def test_malformed_success_response_raises_safe_tmdb_error():
    respx.get(f"{TMDB_URL}/search/multi").mock(return_value=Response(200, content=b"not json"))

    client = TmdbClient("secret-token")
    with pytest.raises(TmdbError, match="^TMDB request failed$") as exc_info:
        await client.search("matrix")
    await client.aclose()

    assert "secret-token" not in str(exc_info.value)


@respx.mock
async def test_invalid_configuration_is_not_cached_and_can_be_retried():
    search = respx.get(f"{TMDB_URL}/search/multi").mock(
        return_value=Response(200, json={"results": [
            {"id": 603, "media_type": "movie", "title": "The Matrix", "poster_path": "/matrix.jpg"},
        ]})
    )
    configuration = respx.get(f"{TMDB_URL}/configuration").mock(
        side_effect=[Response(200, content=b"not json"), configuration_response()]
    )

    client = TmdbClient("secret-token")
    with pytest.raises(TmdbError, match="^TMDB request failed$"):
        await client.search("matrix")
    results = await client.search("matrix")
    await client.aclose()

    assert search.call_count == 2
    assert configuration.call_count == 2
    assert results[0].poster_url == "https://images.example/w342/matrix.jpg"


@respx.mock
async def test_concurrent_poster_lookups_share_one_configuration_request():
    respx.get(f"{TMDB_URL}/search/multi").mock(
        return_value=Response(200, json={"results": [
            {"id": 603, "media_type": "movie", "title": "The Matrix", "poster_path": "/matrix.jpg"},
        ]})
    )

    async def delayed_configuration(_: httpx.Request) -> Response:
        await asyncio.sleep(0)
        return configuration_response()

    configuration = respx.get(f"{TMDB_URL}/configuration").mock(side_effect=delayed_configuration)
    client = TmdbClient("token")
    first, second = await asyncio.gather(client.search("one"), client.search("two"))
    await client.aclose()

    assert configuration.call_count == 1
    assert first[0].poster_url == second[0].poster_url == "https://images.example/w342/matrix.jpg"


@pytest.mark.parametrize("status_code", [401, 429])
@respx.mock
async def test_tmdb_http_errors_hide_token(status_code):
    route = respx.get(f"{TMDB_URL}/search/multi").mock(return_value=Response(status_code))

    client = TmdbClient("secret-token")
    with pytest.raises(TmdbError, match="^TMDB request failed$") as exc_info:
        await client.search("matrix")
    await client.aclose()

    assert "secret-token" not in str(exc_info.value)
    if status_code == 401:
        assert route.call_count == 1


async def test_connect_errors_hide_token():
    transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request)))
    async with httpx.AsyncClient(base_url=TMDB_URL, transport=transport) as http_client:
        client = TmdbClient("secret-token", client=http_client)
        with pytest.raises(TmdbError, match="^TMDB request failed$") as exc_info:
            await client.search("matrix")

    assert "secret-token" not in str(exc_info.value)


async def test_unconfigured_client_does_not_make_http_request():
    client = TmdbClient("")
    with pytest.raises(TmdbError, match="^TMDB is not configured$"):
        await client.search("matrix")
    await client.aclose()

    assert not client.configured


async def test_configured_and_aclose_for_owned_client():
    client = TmdbClient("token")

    assert client.configured
    await client.aclose()
    assert client.client.is_closed
