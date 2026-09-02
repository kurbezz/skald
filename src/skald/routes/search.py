import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from skald.episodes import format_episode_set_input, serialize_episode_set
from skald.indexer.torznab import TorznabError
from skald.parser import parse_release

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")

SORT_FIELDS = {"seeders", "leechers", "size_bytes"}
SORT_DIRECTIONS = {"asc", "desc"}


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
            error = f"Could not reach indexer: {exc}"
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
