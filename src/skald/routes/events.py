from collections import defaultdict
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from skald.db import get_session
from skald.models import (
    NotificationDeliveryAttempt,
    SubscriptionEvent,
    SubscriptionRelease,
    _utcnow,
)

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")
# Search query strings use application/x-www-form-urlencoded spacing so a
# proposal URL can be copied verbatim into the established manual search flow.
templates.env.filters["urlencode"] = quote_plus


@router.get("/events", response_class=HTMLResponse)
async def list_events(request: Request):
    """Show a bounded, audit-friendly history of committed subscription events."""
    with get_session(request.app.state.engine) as session:
        events = session.exec(
            select(SubscriptionEvent)
            .order_by(SubscriptionEvent.created_at.desc(), SubscriptionEvent.id.desc())
            .limit(100)
        ).all()
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
        (event, release_by_id.get(event.subscription_release_id), attempts_by_event_id[event.id])
        for event in events
        if event.id is not None
    ]
    return templates.TemplateResponse(request, "events.html", {"event_rows": event_rows})


@router.post("/events/{event_id}/read")
async def mark_event_read(request: Request, event_id: int):
    with get_session(request.app.state.engine) as session:
        event = session.get(SubscriptionEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        if event.read_at is None:
            event.read_at = _utcnow()
            session.add(event)
        session.commit()
    return RedirectResponse(url="/events", status_code=303)
