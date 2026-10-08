import asyncio
from urllib.parse import quote_plus, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from skald.auth import require_csrf
from skald.flash import set_flash
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
from skald.indexer.base import ReleaseResult
from skald.parser import parse_release
from skald.services.grab import (
    MediaJobCreationError,
    create_media_job,
    find_active_job_for_release,
)
from skald.episodes import serialize_episode_set
from skald.subscriptions import persisted_tv_subscription_scopes, tv_target_episode_numbers
from skald.tmdb import TmdbError

from skald.templating import templates

router = APIRouter()


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


_RELEASES_LIMIT = 100


def _safe_return_to(value: str | None, default: str) -> str:
    """Only allow internal /subscriptions paths as post-action redirect targets."""
    if value and (
        value == "/subscriptions"
        or value.startswith(("/subscriptions/", "/subscriptions?", "/subscriptions#"))
    ):
        if not value.startswith("//") and "\\" not in value and not any(
            ord(ch) < 32 for ch in value
        ):
            return value
    return default


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
async def list_subscriptions(
    request: Request,
    q: str = "",
    unread: int = 0,
    subscription_filter: int | None = Query(default=None, alias="subscription"),
):
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
        releases_query = select(SubscriptionRelease)
        if unread:
            releases_query = releases_query.where(SubscriptionRelease.read_at.is_(None))
        if subscription_filter is not None:
            releases_query = releases_query.where(
                SubscriptionRelease.subscription_id == subscription_filter
            )
        releases = session.exec(
            releases_query.order_by(SubscriptionRelease.discovered_at.desc())
            .limit(_RELEASES_LIMIT + 1)
        ).all()
        releases_truncated = len(releases) > _RELEASES_LIMIT
        releases = releases[:_RELEASES_LIMIT]
        unread_counts = dict(session.exec(
            select(SubscriptionRelease.subscription_id, func.count(SubscriptionRelease.id))
            .where(SubscriptionRelease.read_at.is_(None))
            .group_by(SubscriptionRelease.subscription_id)
        ).all())
        subscriptions = [
            (subscription, unread_counts.get(subscription.id, 0))
            for subscription in subscription_rows
        ]
        tv_ids = [s.id for s in subscription_rows if s.type is MediaType.TV]
        scopes_by_subscription: dict[int, list[TvSubscriptionScope]] = {i: [] for i in tv_ids}
        if tv_ids:
            for scope in session.exec(
                select(TvSubscriptionScope).where(
                    TvSubscriptionScope.subscription_id.in_(tv_ids)
                )
            ).all():
                scopes_by_subscription[scope.subscription_id].append(scope)
        scope_summaries = {
            subscription_id: _scope_summary(scopes)
            for subscription_id, scopes in scopes_by_subscription.items()
        }

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
            "releases_truncated": releases_truncated,
            "releases_limit": _RELEASES_LIMIT,
            "releases_unread_only": bool(unread),
            "releases_subscription_id": subscription_filter,
            "subscription_types": {s.id: s.type.value for s in subscription_rows},
            "subscribed_keys": {(s.type.value, s.tmdb_id) for s in subscription_rows},
            "scope_summaries": scope_summaries,
        },
    )


def _scope_summary(scopes: list[TvSubscriptionScope]) -> dict[str, str]:
    if not scopes:
        return {"state": "none", "label": ""}
    if any(scope.includes_future_content for scope in scopes):
        return {"state": "series", "label": "Entire series"}
    season_numbers = sorted({
        scope.season_number
        for scope in scopes
        if scope.episode_number is None and scope.season_number is not None
    })
    episode_count = sum(1 for scope in scopes if scope.episode_number is not None)
    parts = []
    if season_numbers:
        parts.append(_join_seasons(season_numbers))
    if episode_count:
        parts.append(f"{episode_count} episode{'' if episode_count == 1 else 's'}")
    return {"state": "custom", "label": " · ".join(parts)}


def _join_seasons(season_numbers: list[int]) -> str:
    """'Specials, Season 2' / 'Seasons 1, 3'."""
    if 0 in season_numbers:
        rest = [n for n in season_numbers if n != 0]
        if not rest:
            return "Specials"
        label = f"Season {rest[0]}" if len(rest) == 1 else "Seasons " + ", ".join(map(str, rest))
        return f"Specials, {label}"
    if len(season_numbers) == 1:
        return f"Season {season_numbers[0]}"
    return "Seasons " + ", ".join(map(str, season_numbers))


@router.post("/subscriptions", dependencies=[Depends(require_csrf)])
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

    def _find(session):
        return session.exec(
            select(MediaSubscription)
            .where(MediaSubscription.tmdb_id == tmdb_id)
            .where(MediaSubscription.type == media_type)
        ).first()

    subscription_id = None
    created = False
    with get_session(request.app.state.engine) as session:
        existing = _find(session)
        if existing is None:
            created = True
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
            existing = _find(session)
        if existing is not None:
            subscription_id = existing.id

    if media_type is MediaType.TV and subscription_id is not None:
        response = RedirectResponse(
            url=f"/subscriptions/{subscription_id}?setup=1", status_code=303
        )
        if created:
            set_flash(
                response,
                f"Subscribed to “{media.title}”. Choose seasons to watch.",
                "info",
            )
        return response
    response = RedirectResponse(url="/subscriptions", status_code=303)
    if created:
        set_flash(response, f"Subscribed to “{media.title}”.", "success")
    return response


@router.get("/subscriptions/{subscription_id}/seasons/{season_number}")
async def tv_subscription_season(
    request: Request, subscription_id: int, season_number: int
) -> JSONResponse:
    """Expose validated TMDB episodes for the detail page's season expander."""
    try:
        with get_session(request.app.state.engine) as session:
            subscription = _tv_subscription_or_404(session, subscription_id)
            tmdb_id = subscription.tmdb_id
    except HTTPException as exc:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    try:
        season = await request.app.state.tmdb.get_tv_season(tmdb_id, season_number)
    except TmdbError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    if season is None:
        return JSONResponse({"error": "TMDB season not found"}, status_code=404)

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
async def tv_subscription_detail(
    request: Request, subscription_id: int, setup: int = 0, season: int | None = None
):
    """Render the TV-scope detail page; episode lists load only where needed."""
    with get_session(request.app.state.engine) as session:
        subscription = _tv_subscription_or_404(session, subscription_id)
        scopes = session.exec(
            select(TvSubscriptionScope)
            .where(TvSubscriptionScope.subscription_id == subscription_id)
            .order_by(TvSubscriptionScope.season_number, TvSubscriptionScope.episode_number)
        ).all()

    selected_season_ids = {
        scope.tmdb_season_id
        for scope in scopes
        if scope.tmdb_season_id is not None and scope.tmdb_episode_id is None
    }
    selected_episode_ids = {
        scope.tmdb_episode_id for scope in scopes if scope.tmdb_episode_id is not None
    }
    # Seasons that already carry a selection need their episodes to render it.
    selection_season_ids = selected_season_ids | {
        scope.tmdb_season_id
        for scope in scopes
        if scope.tmdb_episode_id is not None and scope.tmdb_season_id is not None
    }

    seasons: list[dict] = []
    seasons_error = None
    try:
        seasons = await _load_season_views(
            request.app.state.tmdb,
            subscription.tmdb_id,
            load_season_ids=selection_season_ids,
            load_season_numbers={season} if season is not None else set(),
            tolerate_season_errors=True,
        )
    except TmdbError as exc:
        seasons_error = str(exc)

    selected_by_season: dict[int, list[int]] = {}
    for scope in scopes:
        if scope.tmdb_episode_id is not None and scope.tmdb_season_id is not None:
            selected_by_season.setdefault(scope.tmdb_season_id, []).append(scope.tmdb_episode_id)
    for view in seasons:
        total = len(view["episodes"]) if view["loaded"] else view["episode_count"]
        if view["tmdb_id"] in selected_season_ids:
            picked = total
        else:
            picked = len([
                e for e in view["episodes"] if e["tmdb_id"] in selected_episode_ids
            ]) if view["loaded"] else len(selected_by_season.get(view["tmdb_id"], []))
        view["total"] = total
        view["picked"] = picked
        view["selected_episode_ids"] = ",".join(
            str(i) for i in sorted(selected_by_season.get(view["tmdb_id"], []))
        )

    return templates.TemplateResponse(
        request,
        "subscription_detail.html",
        {
            "subscription": subscription,
            "seasons": seasons,
            "seasons_error": seasons_error,
            "setup": bool(setup),
            "scopes": scopes,
            "has_scope": bool(scopes),
            "series_scope_active": any(scope.includes_future_content for scope in scopes),
            "selected_season_ids": selected_season_ids,
            "selected_episode_ids": selected_episode_ids,
        },
    )


async def _fetch_season_details(tmdb, tmdb_series_id: int, numbers, *, tolerate: bool = False) -> dict:
    """Fetch season episode lists concurrently (cached by the client)."""
    numbers = sorted(numbers)
    semaphore = asyncio.Semaphore(4)

    async def fetch(season_number: int):
        async with semaphore:
            return await tmdb.get_tv_season(tmdb_series_id, season_number)

    results = await asyncio.gather(
        *(fetch(number) for number in numbers), return_exceptions=tolerate
    )
    return {
        number: (None if isinstance(result, BaseException) else result)
        for number, result in zip(numbers, results)
    }


async def _load_season_views(
    tmdb,
    tmdb_series_id: int,
    *,
    load_season_ids=None,
    load_season_numbers=None,
    tolerate_season_errors: bool = False,
) -> list[dict]:
    """Build season views; ``load_*`` of ``None`` means every season (legacy)."""
    seasons = await tmdb.get_tv_seasons(tmdb_series_id)
    if load_season_ids is None and load_season_numbers is None:
        wanted = {season.season_number for season in seasons}
    else:
        wanted = {
            season.season_number
            for season in seasons
            if season.tmdb_id in (load_season_ids or ())
            or season.season_number in (load_season_numbers or ())
        }
    details = await _fetch_season_details(
        tmdb, tmdb_series_id, wanted, tolerate=tolerate_season_errors
    )
    views = []
    for season in seasons:
        detail = details.get(season.season_number)
        loaded = detail is not None
        views.append({
            "tmdb_id": season.tmdb_id,
            "number": season.season_number,
            "name": season.name,
            "episode_count": season.episode_count,
            "loaded": loaded,
            "episodes": [
                {"tmdb_id": ep.tmdb_id, "number": ep.episode_number, "name": ep.name}
                for ep in (detail.episodes if detail is not None else [])
            ],
        })
    return views


@router.post("/subscriptions/{subscription_id}/scope", dependencies=[Depends(require_csrf)])
async def save_tv_subscription_scope(
    request: Request,
    subscription_id: int,
    scope_mode: str = Form(),
    season_ids: list[int] = Form(default=[]),
    episode_ids: list[int] = Form(default=[]),
    loaded_season_ids: list[int] = Form(default=[]),
    lazy: int = Form(default=0),
):
    with get_session(request.app.state.engine) as session:
        subscription = _tv_subscription_or_404(session, subscription_id)
        series_id = subscription.tmdb_id
    if scope_mode not in ("series", "manual"):
        raise HTTPException(status_code=400, detail="Unknown scope mode")

    new_rows: list[TvSubscriptionScope] = []
    unloaded_season_ids: set[int] = set()
    if scope_mode == "series":
        new_rows.append(TvSubscriptionScope(
            subscription_id=subscription_id,
            tmdb_series_id=series_id,
            includes_future_content=True,
        ))
    else:
        tmdb = request.app.state.tmdb
        try:
            if lazy:
                # Only the seasons whose episodes the form rendered are
                # authoritative for episode picks; the rest are preserved.
                seasons = await _load_season_views(
                    tmdb, series_id, load_season_ids=set(loaded_season_ids),
                    load_season_numbers=set(),
                )
            else:
                seasons = await _load_season_views(tmdb, series_id)
        except TmdbError as exc:
            return _tmdb_error_page(request, exc)
        season_by_id = {season["tmdb_id"]: season for season in seasons}
        episode_by_id = {
            episode["tmdb_id"]: (season, episode)
            for season in seasons
            for episode in season["episodes"]
        }
        selected_seasons = set(season_ids)
        selected_episodes = set(episode_ids)
        if (
            not selected_seasons <= season_by_id.keys()
            or not set(loaded_season_ids) <= season_by_id.keys()
            or not selected_episodes <= episode_by_id.keys()
        ):
            raise HTTPException(status_code=400, detail="Unknown season or episode")
        if lazy:
            unloaded_season_ids = set(season_by_id) - set(loaded_season_ids)
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
        # Seasons whose episode list was not loaded keep their stored episode
        # picks unless a whole-season selection now supersedes them.
        unloaded_season_ids -= set(season_ids)

    with get_session(request.app.state.engine) as session:
        subscription = _tv_subscription_or_404(session, subscription_id)
        delete_statement = delete(TvSubscriptionScope).where(
            TvSubscriptionScope.subscription_id == subscription_id
        )
        if unloaded_season_ids:
            delete_statement = delete_statement.where(
                ~(
                    TvSubscriptionScope.tmdb_episode_id.is_not(None)
                    & TvSubscriptionScope.tmdb_season_id.in_(unloaded_season_ids)
                )
            )
        session.execute(delete_statement)
        session.add_all(new_rows)
        subscription.next_check_at = _utcnow()
        session.add(subscription)
        session.commit()
    response = RedirectResponse(url=f"/subscriptions/{subscription_id}", status_code=303)
    return set_flash(response, "Saved season selection.", "success")


@router.post("/subscriptions/{subscription_id}/toggle", dependencies=[Depends(require_csrf)])
async def toggle_subscription(
    request: Request, subscription_id: int, return_to: str = Form(default="")
):
    with get_session(request.app.state.engine) as session:
        subscription = _subscription_or_404(session, subscription_id)
        subscription.is_active = not subscription.is_active
        if subscription.is_active:
            subscription.next_check_at = _utcnow()
        session.add(subscription)
        session.commit()
        message = (
            f"Resumed “{subscription.title}”."
            if subscription.is_active
            else f"Paused “{subscription.title}”."
        )
    response = RedirectResponse(url=_safe_return_to(return_to, "/subscriptions"), status_code=303)
    return set_flash(response, message, "success")


@router.post(
    "/subscriptions/{subscription_id}/auto-download", dependencies=[Depends(require_csrf)]
)
async def toggle_subscription_auto_download(
    request: Request, subscription_id: int, return_to: str = Form(default="")
):
    with get_session(request.app.state.engine) as session:
        subscription = _subscription_or_404(session, subscription_id)
        subscription.auto_download = not subscription.auto_download
        if subscription.auto_download:
            subscription.next_check_at = _utcnow()
        session.add(subscription)
        session.commit()
        message = (
            f"Auto-download on for “{subscription.title}”."
            if subscription.auto_download
            else f"Auto-download off for “{subscription.title}”."
        )
    target = _safe_return_to(return_to, "/subscriptions")
    referer = request.headers.get("referer")
    if (
        target == "/subscriptions"
        and referer
        and urlparse(referer).path == f"/subscriptions/{subscription_id}"
    ):
        target = f"/subscriptions/{subscription_id}"
    return set_flash(RedirectResponse(url=target, status_code=303), message, "success")


@router.post("/subscriptions/{subscription_id}/releases/read", dependencies=[Depends(require_csrf)])
async def mark_releases_read(
    request: Request, subscription_id: int, return_to: str = Form(default="")
):
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
    return RedirectResponse(url=_safe_return_to(return_to, "/subscriptions"), status_code=303)


@router.post("/subscriptions/{subscription_id}/delete", dependencies=[Depends(require_csrf)])
async def delete_subscription(request: Request, subscription_id: int):
    with get_session(request.app.state.engine) as session:
        subscription = _subscription_or_404(session, subscription_id)
        title = subscription.title
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
    response = RedirectResponse(url="/subscriptions", status_code=303)
    return set_flash(
        response, f"Deleted subscription “{title}”. Downloaded files were kept.", "success"
    )


@router.post("/subscriptions/releases/{release_id}/grab", dependencies=[Depends(require_csrf)])
async def grab_subscription_release(request: Request, release_id: int):
    """Queue a stored Latest-releases row directly, using its subscription."""
    with get_session(request.app.state.engine) as session:
        release = session.get(SubscriptionRelease, release_id)
        if release is None:
            raise HTTPException(status_code=404, detail="Release not found")
        subscription = session.get(MediaSubscription, release.subscription_id)
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        release_title = release.release_title
        download_url = release.download_url
        common = dict(
            media_type=subscription.type,
            title=subscription.title,
            source_subscription_id=subscription.id,
            source_subscription_release_id=release.id,
            settings=request.app.state.settings,
        )

        def mark_read() -> None:
            stored = session.get(SubscriptionRelease, release_id)
            if stored is not None and stored.read_at is None:
                stored.read_at = _utcnow()
                session.add(stored)
                session.commit()

        if subscription.type is MediaType.TV:
            scopes = persisted_tv_subscription_scopes(session, release)
            targets = tv_target_episode_numbers(release_title, scopes)
            if not targets:
                response = RedirectResponse(
                    url=f"/search?q={quote_plus(release_title)}&type=tv", status_code=303
                )
                return set_flash(
                    response, "Pick episodes in Search for this release.", "warning"
                )
            common.update(
                season=parse_release(release_title)["season"],
                episode=targets[0],
                episode_set=serialize_episode_set(list(targets)),
                target_episode_numbers=list(targets),
            )
        else:
            common["year"] = subscription.year

        existing = find_active_job_for_release(session, release_title, download_url)
        if existing is not None:
            existing_id = existing.id
            mark_read()
            response = RedirectResponse(url=f"/jobs/{existing_id}", status_code=303)
            return set_flash(
                response, f"“{release_title}” is already in the queue.", "info"
            )

        try:
            job = create_media_job(
                session,
                request.app.state.qbit,
                ReleaseResult(release_title, "subscription", 0, 0, 0, download_url),
                **common,
            )
        except MediaJobCreationError as exc:
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "title": "Failed to add torrent",
                    "detail": str(exc),
                    "back_url": "/subscriptions#releases",
                    "back_label": "Back to releases",
                },
                status_code=502,
            )
        job_id = job.id
        mark_read()

    response = RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    return set_flash(response, f"Added “{release_title}” to the queue.", "success")
