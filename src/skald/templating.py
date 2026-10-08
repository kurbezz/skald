"""Shared Jinja environment for all server-rendered pages."""

from datetime import datetime, timezone
from urllib.parse import quote_plus

import logging

from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, select
from starlette.requests import Request

from skald.auth import SESSION_COOKIE_NAME, create_csrf_token
from skald.config import get_settings
from skald.flash import read_flash
from skald.models import JobStatus, MediaJob, SubscriptionEvent

logger = logging.getLogger(__name__)


def _nav_counts(request: Request) -> dict[str, int]:
    """Cheap COUNT queries for the nav badges; zero when there is no app state."""
    counts = {"nav_attention_count": 0, "nav_unread_events": 0}
    if request.url.path == "/login":
        return counts
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return counts
    try:
        with Session(engine) as session:
            counts["nav_attention_count"] = session.exec(
                select(func.count(MediaJob.id))
                .where(MediaJob.status.in_((JobStatus.NEEDS_ATTENTION, JobStatus.FAILED)))
                .where(MediaJob.hidden_at.is_(None))
            ).one()
            counts["nav_unread_events"] = session.exec(
                select(func.count(SubscriptionEvent.id)).where(SubscriptionEvent.read_at.is_(None))
            ).one()
    except Exception:  # noqa: BLE001 - badges must never break page rendering
        logger.warning("Could not compute nav counts", exc_info=True)
    return counts


def _csrf_context(request: Request) -> dict[str, object]:
    """Expose ``csrf_token`` and ``auth_enabled`` to every template."""
    settings = get_settings()
    return {
        **_nav_counts(request),
        "csrf_token": create_csrf_token(request.cookies.get(SESSION_COOKIE_NAME)),
        "auth_enabled": bool(settings.auth_username and settings.auth_password),
        "flash": read_flash(request),
    }


templates = Jinja2Templates(
    directory="src/skald/templates", context_processors=[_csrf_context]
)
# Search query strings use application/x-www-form-urlencoded spacing so a
# proposal URL can be copied verbatim into the established manual search flow.
templates.env.filters["urlencode"] = quote_plus


def _as_utc(value: datetime) -> datetime:
    # SQLite round-trips timestamps as naive values; they are always stored in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def format_datetime(value: datetime | None) -> str:
    """Human-readable UTC timestamp without seconds/microseconds, e.g. ``2026-10-08 20:16 UTC``."""
    if value is None:
        return "—"
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def iso_datetime(value: datetime | None) -> str:
    """Machine-readable value for ``<time datetime="...">``."""
    if value is None:
        return ""
    return _as_utc(value).isoformat(timespec="seconds")


templates.env.filters["dt"] = format_datetime
templates.env.filters["isodt"] = iso_datetime
