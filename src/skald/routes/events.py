from collections import defaultdict
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import select

from skald.auth import SESSION_COOKIE_NAME, create_csrf_token, verify_csrf_form
from skald.db import get_session
from skald.quality import rank_label_tables
from skald.templating import templates
from skald.models import (
    NotificationDeliveryAttempt,
    SubscriptionEvent,
    SubscriptionRelease,
    _utcnow,
)

router = APIRouter()


PAGE_SIZE = 50
_RES_LABELS, _AUDIO_LABELS, _HDR_LABELS = rank_label_tables()


def _safe_return_to(value: str | None) -> str:
    """Only allow same-site /events URLs; anything else falls back to /events."""
    if value and value.startswith("/events") and not value.startswith("//") and "\\" not in value:
        rest = value[len("/events"):]
        if rest == "" or rest[0] in "?":
            return value
    return "/events"


def _label(table: list[str], rank: int) -> str:
    return table[rank] if 0 <= rank < len(table) else "Unknown"


def _quality_names(score: list[int] | None) -> dict[str, str] | None:
    if not score or len(score) < 3:
        return None
    return {
        "resolution": _label(_RES_LABELS, score[0]),
        "audio": _label(_AUDIO_LABELS, score[1]),
        "hdr": _label(_HDR_LABELS, score[2]),
    }


def _events_url(page: int, unread: bool, subscription: int | None) -> str:
    params: dict[str, object] = {}
    if unread:
        params["unread"] = 1
    if subscription is not None:
        params["subscription"] = subscription
    if page > 1:
        params["page"] = page
    return "/events" + (f"?{urlencode(params)}" if params else "")


@router.get("/events", response_class=HTMLResponse)
async def list_events(
    request: Request, page: str = "1", unread: str = "", subscription: str = ""
):
    """Show a paginated, filterable history of committed subscription events."""
    page_number = int(page) if page.isdigit() and int(page) >= 1 else 1
    unread_only = unread == "1"
    subscription_id = int(subscription) if subscription.isdigit() else None
    with get_session(request.app.state.engine) as session:
        query = select(SubscriptionEvent)
        if unread_only:
            query = query.where(SubscriptionEvent.read_at.is_(None))
        if subscription_id is not None:
            query = query.where(SubscriptionEvent.subscription_id == subscription_id)
        fetched = session.exec(
            query.order_by(SubscriptionEvent.created_at.desc(), SubscriptionEvent.id.desc())
            .offset((page_number - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        ).all()
        has_older = len(fetched) > PAGE_SIZE
        events = fetched[:PAGE_SIZE]
        release_ids = {
            event.subscription_release_id
            for event in events
            if event.subscription_release_id is not None
        }
        releases = (
            session.exec(
                select(SubscriptionRelease).where(SubscriptionRelease.id.in_(release_ids))
            ).all()
            if release_ids
            else []
        )
        event_ids = [event.id for event in events if event.id is not None]
        attempts = (
            session.exec(
                select(NotificationDeliveryAttempt)
                .where(NotificationDeliveryAttempt.event_id.in_(event_ids))
                .order_by(NotificationDeliveryAttempt.attempted_at)
            ).all()
            if event_ids
            else []
        )

    release_by_id = {release.id: release for release in releases}
    attempts_by_event_id: dict[int, list[NotificationDeliveryAttempt]] = defaultdict(list)
    for attempt in attempts:
        if attempt.event_id is not None:
            attempts_by_event_id[attempt.event_id].append(attempt)
    event_rows = [
        (
            event,
            release_by_id.get(event.subscription_release_id),
            attempts_by_event_id[event.id],
            _quality_names(event.prior_quality_score),
            _quality_names(event.current_quality_score),
        )
        for event in events
        if event.id is not None
    ]
    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "event_rows": event_rows,
            "page": page_number,
            "unread_only": unread_only,
            "subscription_id": subscription_id,
            "current_url": _events_url(page_number, unread_only, subscription_id),
            "newer_url": _events_url(page_number - 1, unread_only, subscription_id)
            if page_number > 1
            else None,
            "older_url": _events_url(page_number + 1, unread_only, subscription_id)
            if has_older
            else None,
            "toggle_unread_url": _events_url(1, not unread_only, subscription_id),
            "csrf_token": create_csrf_token(request.cookies.get(SESSION_COOKIE_NAME)),
        },
    )


@router.post("/events/{event_id}/read")
async def mark_event_read(request: Request, event_id: int):
    form = await request.form()
    if not await verify_csrf_form(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return_to = _safe_return_to(form.get("return_to") if form else None)

    with get_session(request.app.state.engine) as session:
        event = session.get(SubscriptionEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        if event.read_at is None:
            event.read_at = _utcnow()
            session.add(event)
        session.commit()
    return RedirectResponse(url=f"{return_to.split('#', 1)[0]}#event-{event_id}", status_code=303)
