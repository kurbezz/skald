import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel

from skald.auth import require_auth, safe_back_url
from skald.config import get_settings
from skald.db import get_engine, get_session, migrate_schema
from skald.flash import FLASH_COOKIE_NAME
from skald.indexer.torznab import TorznabIndexer
from skald.qbittorrent import QbittorrentClient
from skald.routes.auth import router as auth_router
from skald.routes.events import router as events_router
from skald.routes.jobs import router as jobs_router
from skald.routes.quality import router as quality_router
from skald.routes.search import router as search_router
from skald.routes.subscriptions import router as subscriptions_router
from skald.templating import templates
from skald.tmdb import TmdbClient
from skald.services.notifications import NotificationDeliveryService
from skald.worker import worker_loop


logger = logging.getLogger(__name__)

_SAFE_FETCH_SITES = {"same-origin", "none"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_STATUS_TITLES = {
    403: "Session check failed — reload the page and try again",
    404: "Not found",
    409: "Conflict",
    422: "Some fields are invalid",
    500: "Something went wrong",
}


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _html_error(
    request: Request, status_code: int, detail: str, title: str | None = None
) -> Response:
    back_url = safe_back_url(request)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "title": title or _STATUS_TITLES.get(status_code, f"Error {status_code}"),
            "detail": detail,
            "back_url": back_url,
            "back_label": "Go back" if back_url != "/jobs" else "Back to jobs",
        },
        status_code=status_code,
    )


def create_app() -> FastAPI:
    settings = get_settings()
    engine = get_engine(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        SQLModel.metadata.create_all(engine)
        migrate_schema(engine)

        app.state.settings = settings
        app.state.engine = engine
        app.state.notification_delivery = NotificationDeliveryService(
            lambda: get_session(engine), settings
        )
        app.state.indexer = TorznabIndexer(settings.jackett_url, settings.jackett_api_key)
        app.state.qbit = QbittorrentClient(
            settings.qbit_host, settings.qbit_user, settings.qbit_pass
        )
        tmdb_client = TmdbClient(settings.tmdb_read_access_token)
        app.state.tmdb = tmdb_client

        task = asyncio.create_task(
            worker_loop(
                session_factory=lambda: get_session(engine),
                qbit=app.state.qbit,
                movies_root=settings.movies_library_path,
                tv_root=settings.tv_library_path,
                poll_interval_seconds=settings.worker_poll_interval_seconds,
                indexer=app.state.indexer,
                subscription_check_interval_seconds=settings.subscription_check_interval_seconds,
                settings=settings,
                delivery_service=app.state.notification_delivery,
            )
        )
        try:
            yield
        finally:
            try:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            finally:
                await tmdb_client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="src/skald/static"), name="static")
    app.include_router(auth_router)
    app.include_router(search_router, dependencies=[Depends(require_auth)])
    app.include_router(jobs_router, dependencies=[Depends(require_auth)])
    app.include_router(quality_router, dependencies=[Depends(require_auth)])
    app.include_router(subscriptions_router, dependencies=[Depends(require_auth)])
    app.include_router(events_router, dependencies=[Depends(require_auth)])

    @app.middleware("http")
    async def reject_cross_site_writes(request: Request, call_next):
        # Defense in depth next to the CSRF token: browsers label cross-site
        # requests; a missing header (curl, old browsers) is allowed.
        site = request.headers.get("sec-fetch-site")
        if (
            request.method not in _SAFE_METHODS
            and site is not None
            and site.strip().lower() not in _SAFE_FETCH_SITES
        ):
            detail = "Cross-site request rejected"
            if _wants_html(request):
                return _html_error(request, 403, detail)
            return Response(
                content='{"detail":"%s"}' % detail,
                status_code=403,
                media_type="application/json",
            )
        return await call_next(request)

    @app.middleware("http")
    async def consume_flash(request: Request, call_next):
        # A flash survives redirects and is cleared once a page has been rendered.
        response = await call_next(request)
        if (
            FLASH_COOKIE_NAME in request.cookies
            and not 300 <= response.status_code < 400
            and response.headers.get("content-type", "").startswith("text/html")
        ):
            response.delete_cookie(FLASH_COOKIE_NAME)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def html_http_exception(request: Request, exc: StarletteHTTPException):
        has_location = any(k.lower() == "location" for k in (exc.headers or {}))
        if has_location or not _wants_html(request):
            return await http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, str) else ""
        return _html_error(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def html_validation_error(request: Request, exc: RequestValidationError):
        if not _wants_html(request):
            return await request_validation_exception_handler(request, exc)
        fields = []
        for error in exc.errors():
            loc = [str(p) for p in error.get("loc", ()) if p not in ("body", "query", "path")]
            name = loc[-1] if loc else ""
            if name and name not in fields:
                fields.append(name)
        detail = "Check: " + ", ".join(fields) if fields else "Invalid request"
        return _html_error(request, 422, detail)

    @app.exception_handler(Exception)
    async def html_server_error(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        if _wants_html(request):
            return _html_error(request, 500, "An unexpected error occurred. See the server log.")
        return Response(
            content='{"detail":"Internal Server Error"}',
            status_code=500,
            media_type="application/json",
        )

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/jobs")

    return app


app = create_app()
