from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from skald.db import get_session
from skald.models import (
    MediaSubscription,
    MediaType,
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
        seasons = await request.app.state.tmdb.get_tv_seasons(subscription.tmdb_id)
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
        },
    )


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
        session.add(subscription)
        session.commit()
    return RedirectResponse(url="/subscriptions", status_code=303)


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
        session.execute(
            delete(SubscriptionRelease).where(SubscriptionRelease.subscription_id == subscription_id)
        )
        session.delete(subscription)
        session.commit()
    return RedirectResponse(url="/subscriptions", status_code=303)
