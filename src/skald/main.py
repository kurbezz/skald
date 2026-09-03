import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel

from skald.auth import require_auth
from skald.config import get_settings
from skald.db import get_engine, get_session, migrate_schema
from skald.indexer.torznab import TorznabIndexer
from skald.qbittorrent import QbittorrentClient
from skald.routes.auth import router as auth_router
from skald.routes.jobs import router as jobs_router
from skald.routes.quality import router as quality_router
from skald.routes.search import router as search_router
from skald.routes.subscriptions import router as subscriptions_router
from skald.tmdb import TmdbClient
from skald.worker import worker_loop


def create_app() -> FastAPI:
    settings = get_settings()
    engine = get_engine(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        SQLModel.metadata.create_all(engine)
        migrate_schema(engine)

        app.state.settings = settings
        app.state.engine = engine
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

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/jobs")

    return app


app = create_app()
