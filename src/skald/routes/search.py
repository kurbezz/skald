import logging
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from skald.episodes import format_episode_set_input, serialize_episode_set
from skald.indexer.torznab import TorznabError
from skald.parser import parse_release

from skald.templating import templates

router = APIRouter()

logger = logging.getLogger(__name__)

MEDIA_TYPES = {"movie", "tv"}
SORT_FIELDS = {"seeders", "leechers", "size_bytes"}
SORT_DIRECTIONS = {"asc", "desc"}


def describe_indexer_error(exc: httpx.HTTPError, base_url: str = "") -> str:
    """User-safe indexer failure text; never includes the request URL (holds the API key)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return (
            f"Indexer responded with HTTP {exc.response.status_code} "
            "— check JACKETT_URL / API key"
        )
    if isinstance(exc, httpx.TimeoutException):
        return "Indexer timed out"
    if isinstance(exc, httpx.ConnectError):
        parts = urlsplit(base_url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "the configured address"
        return f"Could not connect to indexer at {origin}"
    return "Could not reach indexer"


def needs_metadata_review(guess: dict, media_type: str) -> bool:
    required_fields = ("title", "season", "episode") if media_type == "tv" else ("title", "year")
    return any(guess[field] is None for field in required_fields)


def episode_set_display_label(episode_set: tuple[int, ...]) -> str:
    """Format normalized episodes for the compact search-result label."""
    ranges = format_episode_set_input(episode_set)
    return ",".join(
        "-".join(f"E{int(episode):02d}" for episode in episode_range.split("-"))
        for episode_range in ranges.split(",")
    )


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    type: str = "movie",
    sort: str = "seeders",
    direction: str = "desc",
):
    if type not in MEDIA_TYPES:
        type = "movie"
    if sort not in SORT_FIELDS or direction not in SORT_DIRECTIONS:
        sort, direction = "seeders", "desc"
    results = []
    error = None
    if q:
        indexer = request.app.state.indexer
        try:
            releases = await indexer.search(q)
        except TorznabError as exc:
            error = str(exc)
        except httpx.HTTPError as exc:
            logger.warning("Indexer request failed: %r", exc, exc_info=True)
            error = describe_indexer_error(exc, getattr(indexer, "base_url", ""))
        else:
            releases = sorted(
                releases,
                key=lambda release: getattr(release, sort),
                reverse=direction == "desc",
            )
            for release in releases:
                guess = parse_release(release.title)
                episode_set = guess.get("episode_set", ())
                episode_set_value = (
                    serialize_episode_set(episode_set) if len(episode_set) > 1 else ""
                )
                results.append(
                    {
                        "release": release,
                        "guess": guess,
                        "needs_review": needs_metadata_review(guess, type),
                        "episode_set_value": episode_set_value,
                        "episode_set_input": (
                            format_episode_set_input(episode_set) if episode_set_value else ""
                        ),
                        "episode_label": (
                            episode_set_display_label(episode_set) if episode_set_value else ""
                        ),
                    }
                )
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": q,
            "type": type,
            "sort": sort,
            "direction": direction,
            "results": results,
            "error": error,
        },
    )
