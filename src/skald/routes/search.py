import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from skald.indexer.torznab import TorznabError
from skald.parser import parse_release

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", type: str = "movie"):
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
            for release in releases:
                guess = parse_release(release.title)
                results.append({"release": release, "guess": guess})
    return templates.TemplateResponse(
        request, "search.html", {"query": q, "type": type, "results": results, "error": error}
    )
