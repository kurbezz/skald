import asyncio
from dataclasses import dataclass
import random
import re
from typing import Any

import httpx

from skald.models import MediaType


TMDB_API_URL = "https://api.themoviedb.org/3"
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.1
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class TmdbError(RuntimeError):
    """A safe, user-facing TMDB client error."""


@dataclass(frozen=True)
class TmdbMedia:
    tmdb_id: int
    type: MediaType
    title: str
    original_title: str | None
    year: int | None
    poster_url: str | None


@dataclass(frozen=True)
class TmdbSeason:
    tmdb_id: int
    season_number: int
    name: str
    air_date: str | None
    episode_count: int


@dataclass(frozen=True)
class TmdbEpisode:
    tmdb_id: int
    episode_number: int
    name: str
    air_date: str | None


@dataclass(frozen=True)
class TmdbTvSeason:
    tmdb_id: int
    season_number: int
    name: str
    air_date: str | None
    episodes: list[TmdbEpisode]


class TmdbClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.token = token
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(base_url=TMDB_API_URL, timeout=10)
        self._configuration_loaded = False
        self._poster_base_url: str | None = None
        self._configuration_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def search(self, query: str) -> list[TmdbMedia]:
        response = await self._get(
            "/search/multi",
            params={"query": query, "language": "en-US", "include_adult": "false"},
        )
        payload = self._json(response)
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list):
            return []

        media = []
        for item in results:
            normalized = await self._normalize(item)
            if normalized is not None:
                media.append(normalized)
        return media

    async def get_media(self, tmdb_id: int, media_type: MediaType) -> TmdbMedia | None:
        if media_type not in (MediaType.MOVIE, MediaType.TV):
            return None

        response = await self._get(f"/{media_type.value}/{tmdb_id}")
        payload = self._json(response)
        if not isinstance(payload, dict):
            return None
        return await self._normalize({**payload, "media_type": media_type.value})

    async def get_tv_seasons(self, tmdb_id: int) -> list[TmdbSeason]:
        """Return the season metadata for a TV series, including season zero."""
        response = await self._get(f"/tv/{tmdb_id}", params={"language": "en-US"})
        payload = self._json(response)
        seasons = payload.get("seasons") if isinstance(payload, dict) else None
        if not isinstance(seasons, list):
            return []
        return [season for item in seasons if (season := self._normalize_season(item)) is not None]

    async def get_tv_season(self, tmdb_id: int, season_number: int) -> TmdbTvSeason | None:
        """Return TMDB's authoritative episode list for one TV season."""
        response = await self._get(
            f"/tv/{tmdb_id}/season/{season_number}", params={"language": "en-US"}
        )
        payload = self._json(response)
        season = self._normalize_season(payload, require_episode_count=False)
        if season is None or season.season_number != season_number:
            return None

        episodes = payload.get("episodes") if isinstance(payload, dict) else None
        if not isinstance(episodes, list):
            return None
        return TmdbTvSeason(
            tmdb_id=season.tmdb_id,
            season_number=season.season_number,
            name=season.name,
            air_date=season.air_date,
            episodes=[
                episode
                for item in episodes
                if (episode := self._normalize_episode(item)) is not None
            ],
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        if not self.configured:
            raise TmdbError("TMDB is not configured")
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.get(
                    path,
                    params=params,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code not in RETRYABLE_STATUS_CODES
                    or attempt == MAX_RETRY_ATTEMPTS - 1
                ):
                    raise TmdbError("TMDB request failed") from exc
            except httpx.TransportError as exc:
                if attempt == MAX_RETRY_ATTEMPTS - 1:
                    raise TmdbError("TMDB request failed") from exc
            except httpx.HTTPError as exc:
                raise TmdbError("TMDB request failed") from exc

            delay = RETRY_BACKOFF_SECONDS * (2**attempt)
            await asyncio.sleep(delay + random.uniform(0, delay))

        raise TmdbError("TMDB request failed")

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise TmdbError("TMDB request failed") from exc

    async def _normalize(self, item: Any) -> TmdbMedia | None:
        if not isinstance(item, dict):
            return None

        media_type = self._media_type(item.get("media_type"))
        tmdb_id = item.get("id")
        if media_type is None or not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
            return None

        is_movie = media_type is MediaType.MOVIE
        title = item.get("title" if is_movie else "name")
        if not isinstance(title, str) or not title.strip():
            return None

        original_title = item.get("original_title" if is_movie else "original_name")
        if not isinstance(original_title, str) or not original_title.strip():
            original_title = None

        date = item.get("release_date" if is_movie else "first_air_date")
        year = self._year(date)
        poster_url = await self._poster_url(item.get("poster_path"))
        return TmdbMedia(tmdb_id, media_type, title, original_title, year, poster_url)

    @staticmethod
    def _normalize_season(
        item: Any, *, require_episode_count: bool = True
    ) -> TmdbSeason | None:
        if not isinstance(item, dict):
            return None
        tmdb_id = item.get("id")
        season_number = item.get("season_number")
        name = item.get("name")
        episode_count = item.get("episode_count")
        if (
            not _is_positive_int(tmdb_id)
            or not _is_nonnegative_int(season_number)
            or not isinstance(name, str)
            or not name.strip()
            or (require_episode_count and not _is_nonnegative_int(episode_count))
        ):
            return None
        air_date = item.get("air_date")
        return TmdbSeason(
            tmdb_id=tmdb_id,
            season_number=season_number,
            name=name,
            air_date=air_date if isinstance(air_date, str) and air_date else None,
            episode_count=episode_count if _is_nonnegative_int(episode_count) else 0,
        )

    @staticmethod
    def _normalize_episode(item: Any) -> TmdbEpisode | None:
        if not isinstance(item, dict):
            return None
        tmdb_id = item.get("id")
        episode_number = item.get("episode_number")
        name = item.get("name")
        if (
            not _is_positive_int(tmdb_id)
            or not _is_positive_int(episode_number)
            or not isinstance(name, str)
            or not name.strip()
        ):
            return None
        air_date = item.get("air_date")
        return TmdbEpisode(
            tmdb_id=tmdb_id,
            episode_number=episode_number,
            name=name,
            air_date=air_date if isinstance(air_date, str) and air_date else None,
        )

    @staticmethod
    def _media_type(value: Any) -> MediaType | None:
        try:
            return MediaType(value)
        except ValueError:
            return None

    @staticmethod
    def _year(value: Any) -> int | None:
        if not isinstance(value, str):
            return None
        match = re.match(r"(\d{4})", value)
        return int(match.group(1)) if match else None

    async def _poster_url(self, path: Any) -> str | None:
        if not isinstance(path, str) or not path:
            return None
        if not self._configuration_loaded:
            await self._load_configuration()
        return f"{self._poster_base_url}w342{path}" if self._poster_base_url else None

    async def _load_configuration(self) -> None:
        async with self._configuration_lock:
            if self._configuration_loaded:
                return
            response = await self._get("/configuration")
            payload = self._json(response)
            images = payload.get("images") if isinstance(payload, dict) else None
            if not isinstance(images, dict):
                raise TmdbError("TMDB request failed")
            base_url = images.get("secure_base_url")
            poster_sizes = images.get("poster_sizes")
            if not isinstance(base_url, str) or not base_url or not isinstance(poster_sizes, list):
                raise TmdbError("TMDB request failed")
            if "w342" in poster_sizes:
                self._poster_base_url = base_url
            self._configuration_loaded = True


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
