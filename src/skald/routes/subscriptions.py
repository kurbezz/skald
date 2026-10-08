import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from skald.db import get_session
from skald.models import (
    MediaSubscription,
    DownloadedQuality,
    MediaJob,
    MediaType,
    SubscriptionEvent,
    SubscriptionRelease,
    TvSubscriptionScope,
    _utcnow,
)
from skald.tmdb import TmdbError

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")


def _subscription_or_404(session, subscription_id: int) -> MediaSubscription:
    subscription = session.get(MediaSubscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return subscription


def _tv_subscription_or_404(session, subscription_id: int) -> MediaSubscription:
    subscription = _subscription_or_404(session, subscription_id)
    if subscription.type is not MediaType.TV:
        raise HTTPException(status_code=404, detail="TV subscription not found")
    return subscription


def _tmdb_error_page(
    request: Request, error: TmdbError, *, back_url: str = "/subscriptions"
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "title": "TMDB request failed",
            "detail": str(error),
            "back_url": back_url,
            "back_label": "Back to subscriptions",
        },
        status_code=502,
    )


@router.get("/subscriptions", response_class=HTMLResponse)
async def list_subscriptions(request: Request, q: str = ""):
    tmdb = request.app.state.tmdb
    results = []
    error = None
    if q:
        if not tmdb.configured:
            error = "TMDB is not configured"
        else:
            try:
                results = await tmdb.search(q)
            except TmdbError as exc:
                error = str(exc)

    with get_session(request.app.state.engine) as session:
        subscription_rows = session.exec(
            select(MediaSubscription).order_by(MediaSubscription.created_at.desc())
        ).all()
        releases = session.exec(
            select(SubscriptionRelease).order_by(SubscriptionRelease.discovered_at.desc())
            .limit(100)
        ).all()
        unread_counts = dict(session.exec(
            select(SubscriptionRelease.subscription_id, func.count(SubscriptionRelease.id))
            .where(SubscriptionRelease.read_at.is_(None))
            .group_by(SubscriptionRelease.subscription_id)
        ).all())
        subscriptions = [
            (subscription, unread_counts.get(subscription.id, 0))
            for subscription in subscription_rows
        ]

    return templates.TemplateResponse(
        request,
        "subscriptions.html",
        {
            "q": q,
            "catalog_results": results,
            "error": error,
            "tmdb_configured": tmdb.configured,
            "subscriptions": subscriptions,
            "releases": releases,
        },
    )


@router.post("/subscriptions")
async def create_subscription(
    request: Request,
    tmdb_id: int = Form(),
    media_type: MediaType = Form(),
):
    try:
        media = await request.app.state.tmdb.get_media(tmdb_id, media_type)
    except TmdbError as exc:
        return _tmdb_error_page(request, exc)

    if media is None or media.tmdb_id != tmdb_id or media.type != media_type:
        raise HTTPException(status_code=404, detail="TMDB media not found")

    with get_session(request.app.state.engine) as session:
        existing = session.exec(
            select(MediaSubscription)
            .where(MediaSubscription.tmdb_id == tmdb_id)
            .where(MediaSubscription.type == media_type)
        ).first()
        if existing is None:
            session.add(MediaSubscription(
                tmdb_id=media.tmdb_id,
                type=media.type,
                title=media.title,
                original_title=media.original_title,
                year=media.year,
                poster_url=media.poster_url,
            ))
            try:
                session.commit()
            except IntegrityError:
                # The unique constraint makes simultaneous submissions safe.
                session.rollback()

    return RedirectResponse(url="/subscriptions", status_code=303)


@router.get("/subscriptions/{subscription_id}/seasons/{season_number}")
async def tv_subscription_season(
    request: Request, subscription_id: int, season_number: int
) -> JSONResponse:
    """Expose validated TMDB episodes for the detail page's season expander."""
    with get_session(request.app.state.engine) as session:
        subscription = _tv_subscription_or_404(session, subscription_id)

    try:
        season = await request.app.state.tmdb.get_tv_season(subscription.tmdb_id, season_number)
    except TmdbError as exc:
        return _tmdb_error_page(
            request, exc, back_url=f"/subscriptions/{subscription_id}"
        )
    if season is None:
        raise HTTPException(status_code=404, detail="TMDB season not found")

    return JSONResponse({
        "tmdb_id": season.tmdb_id,
        "season_number": season.season_number,
        "name": season.name,
        "air_date": season.air_date,
        "episodes": [
            {
                "tmdb_id": episode.tmdb_id,
                "episode_number": episode.episode_number,
                "name": episode.name,
                "air_date": episode.air_date,
            }
            for episode in season.episodes
        ],
    })


@router.get("/subscriptions/{subscription_id}", response_class=HTMLResponse)
async def tv_subscription_detail(request: Request, subscription_id: int):
    """Render the TV-scope detail page with server-validated TMDB season data."""
    with get_session(request.app.state.engine) as session:
        subscription = _tv_subscription_or_404(session, subscription_id)
        scopes = session.exec(
            select(TvSubscriptionScope)
            .where(TvSubscriptionScope.subscription_id == subscription_id)
            .order_by(TvSubscriptionScope.season_number, TvSubscriptionScope.episode_number)
        ).all()

    try:
        seasons = await _load_season_views(request.app.state.tmdb, subscription.tmdb_id)
    except TmdbError as exc:
        return _tmdb_error_page(
            request, exc, back_url=f"/subscriptions/{subscription_id}"
        )

    return templates.TemplateResponse(
        request,
        "subscription_detail.html",
        {
            "subscription": subscription,
            "seasons": seasons,
            "scopes": scopes,
            "series_scope_active": any(scope.includes_future_content for scope in scopes),
            "selected_season_ids": {
                scope.tmdb_season_id
                for scope in scopes
                if scope.tmdb_season_id is not None and scope.tmdb_episode_id is None
            },
            "selected_episode_ids": {
                scope.tmdb_episode_id for scope in scopes if scope.tmdb_episode_id is not None
            },
        },
    )


async def _load_season_views(tmdb, tmdb_series_id: int) -> list[dict]:
    seasons = await tmdb.get_tv_seasons(tmdb_series_id)
    details = await asyncio.gather(*(
        tmdb.get_tv_season(tmdb_series_id, season.season_number) for season in seasons
    ))
    return [
        {
            "tmdb_id": season.tmdb_id,
            "number": season.season_number,
            "name": season.name,
            "episodes": [
                {"tmdb_id": ep.tmdb_id, "number": ep.episode_number, "name": ep.name}
                for ep in (detail.episodes if detail is not None else [])
            ],
        }
        for season, detail in zip(seasons, details)
    ]


@router.post("/subscriptions/{subscription_id}/scope")
async def save_tv_subscription_scope(
    request: Request,
    subscription_id: int,
    scope_mode: str = Form(),
    season_ids: list[int] = Form(default=[]),
    episode_ids: list[int] = Form(default=[]),
):
    with get_session(request.app.state.engine) as session:
        subscription = _tv_subscription_or_404(session, subscription_id)
        series_id = subscription.tmdb_id
    if scope_mode not in ("series", "manual"):
        raise HTTPException(status_code=400, detail="Unknown scope mode")

    new_rows: list[TvSubscriptionScope] = []
    if scope_mode == "series":
        new_rows.append(TvSubscriptionScope(
            subscription_id=subscription_id,
            tmdb_series_id=series_id,
            includes_future_content=True,
        ))
    else:
        try:
            seasons = await _load_season_views(request.app.state.tmdb, series_id)
        except TmdbError as exc:
            return _tmdb_error_page(
                request, exc, back_url=f"/subscriptions/{subscription_id}"
            )
        season_by_id = {season["tmdb_id"]: season for season in seasons}
        episode_by_id = {
            episode["tmdb_id"]: (season, episode)
            for season in seasons
            for episode in season["episodes"]
        }
        selected_seasons = set(season_ids)
        selected_episodes = set(episode_ids)
        if not selected_seasons <= season_by_id.keys() or not selected_episodes <= episode_by_id.keys():
            raise HTTPException(status_code=400, detail="Unknown season or episode")
        for season_id in sorted(selected_seasons):
            season = season_by_id[season_id]
            new_rows.append(TvSubscriptionScope(
                subscription_id=subscription_id,
                tmdb_series_id=series_id,
                tmdb_season_id=season_id,
                season_number=season["number"],
            ))
        for episode_id in sorted(selected_episodes):
            season, episode = episode_by_id[episode_id]
            if season["tmdb_id"] in selected_seasons:
                continue
            new_rows.append(TvSubscriptionScope(
                subscription_id=subscription_id,
                tmdb_series_id=series_id,
                tmdb_season_id=season["tmdb_id"],
                season_number=season["number"],
                tmdb_episode_id=episode_id,
                episode_number=episode["number"],
            ))

    with get_session(request.app.state.engine) as session:
        subscription = _tv_subscription_or_404(session, subscription_id)
        session.execute(
            delete(TvSubscriptionScope).where(
                TvSubscriptionScope.subscription_id == subscription_id
            )
        )
        session.add_all(new_rows)
        subscription.next_check_at = _utcnow()
        session.add(subscription)
        session.commit()
    return RedirectResponse(url=f"/subscriptions/{subscription_id}", status_code=303)


@router.post("/subscriptions/{subscription_id}/toggle")
async def toggle_subscription(request: Request, subscription_id: int):
    with get_session(request.app.state.engine) as session:
        subscription = _subscription_or_404(session, subscription_id)
        subscription.is_active = not subscription.is_active
        if subscription.is_active:
            subscription.next_check_at = _utcnow()
        session.add(subscription)
        session.commit()
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/{subscription_id}/auto-download")
async def toggle_subscription_auto_download(request: Request, subscription_id: int):
    with get_session(request.app.state.engine) as session:
        subscription = _subscription_or_404(session, subscription_id)
        subscription.auto_download = not subscription.auto_download
        if subscription.auto_download:
            subscription.next_check_at = _utcnow()
        session.add(subscription)
        session.commit()
    target = "/subscriptions"
    referer = request.headers.get("referer")
    if referer and urlparse(referer).path == f"/subscriptions/{subscription_id}":
        target = f"/subscriptions/{subscription_id}"
    return RedirectResponse(url=target, status_code=303)


@router.post("/subscriptions/{subscription_id}/releases/read")
async def mark_releases_read(request: Request, subscription_id: int):
    with get_session(request.app.state.engine) as session:
        _subscription_or_404(session, subscription_id)
        now = _utcnow()
        unread_releases = session.exec(
            select(SubscriptionRelease)
            .where(SubscriptionRelease.subscription_id == subscription_id)
            .where(SubscriptionRelease.read_at.is_(None))
        ).all()
        for release in unread_releases:
            release.read_at = now
            session.add(release)
        session.commit()
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/{subscription_id}/delete")
async def delete_subscription(request: Request, subscription_id: int):
    with get_session(request.app.state.engine) as session:
        subscription = _subscription_or_404(session, subscription_id)
        release_ids = session.exec(
            select(SubscriptionRelease.id).where(
                SubscriptionRelease.subscription_id == subscription_id
            )
        ).all()
        subscription.auto_grabbed_release_id = None
        session.add(subscription)
        session.execute(
            update(MediaJob)
            .where(
                (MediaJob.source_subscription_id == subscription_id)
                | (MediaJob.source_subscription_release_id.in_(release_ids))
            )
            .values(source_subscription_id=None, source_subscription_release_id=None)
        )
        session.execute(
            update(DownloadedQuality)
            .where(DownloadedQuality.subscription_id == subscription_id)
            .values(subscription_id=None)
        )
        session.execute(
            delete(SubscriptionEvent).where(SubscriptionEvent.subscription_id == subscription_id)
        )
        session.execute(
            delete(SubscriptionRelease).where(SubscriptionRelease.subscription_id == subscription_id)
        )
        session.delete(subscription)
        session.commit()
    return RedirectResponse(url="/subscriptions", status_code=303)
