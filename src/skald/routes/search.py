import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from skald.indexer.torznab import TorznabError
from skald.parser import parse_release

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")

SORT_FIELDS = {"seeders", "leechers", "size_bytes"}
SORT_DIRECTIONS = {"asc", "desc"}


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
                results.append({"release": release, "guess": guess})
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
