import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import SQLModel

from skald.config import get_settings
from skald.db import get_engine, get_session
from skald.indexer.torznab import TorznabIndexer
from skald.qbittorrent import QbittorrentClient
from skald.routes.jobs import router as jobs_router
from skald.routes.search import router as search_router
from skald.worker import worker_loop


def create_app() -> FastAPI:
    settings = get_settings()
    engine = get_engine(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        SQLModel.metadata.create_all(engine)

        app.state.settings = settings
        app.state.engine = engine
        app.state.indexer = TorznabIndexer(settings.jackett_url, settings.jackett_api_key)
        app.state.qbit = QbittorrentClient(
            settings.qbit_host, settings.qbit_user, settings.qbit_pass
        )

        task = asyncio.create_task(
            worker_loop(
                session_factory=lambda: get_session(engine),
                qbit=app.state.qbit,
                movies_root=settings.movies_library_path,
                tv_root=settings.tv_library_path,
                poll_interval_seconds=settings.worker_poll_interval_seconds,
            )
        )
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan)
    app.include_router(search_router)
    app.include_router(jobs_router)
    return app


app = create_app()
