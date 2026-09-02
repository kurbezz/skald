from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from skald.parser import parse_release

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", type: str = "movie"):
    results = []
    if q:
        indexer = request.app.state.indexer
        releases = await indexer.search(q)
        for release in releases:
            guess = parse_release(release.title)
            results.append({"release": release, "guess": guess})
    return templates.TemplateResponse(
        request, "search.html", {"query": q, "type": type, "results": results}
    )
