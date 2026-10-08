from sqlmodel import Session

from skald.auth import create_csrf_token
from skald.main import create_app
from skald.models import (
    DeliveryOutcome,
    MediaSubscription,
    MediaType,
    NotificationChannel,
    NotificationDeliveryAttempt,
    SubscriptionEvent,
    SubscriptionEventKind,
)
from fastapi.testclient import TestClient


def _seed(app, count, subscription_ids=1):
    ids = []
    with Session(app.state.engine) as session:
        subs = []
        for n in range(subscription_ids):
            sub = MediaSubscription(tmdb_id=n + 1, type=MediaType.MOVIE, title=f"M{n}")
            session.add(sub)
            subs.append(sub)
        session.commit()
        for i in range(count):
            event = SubscriptionEvent(
                subscription_id=subs[i % len(subs)].id,
                media_type=MediaType.MOVIE,
                kind=SubscriptionEventKind.RELEASE_MATCH,
                dedupe_key=f"k{i}",
                title=f"Event number {i:03d}",
                body="b",
            )
            session.add(event)
            session.commit()
            ids.append(event.id)
        return ids, [s.id for s in subs]


def test_events_pagination_and_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "events.db"))
    app = create_app()
    with TestClient(app) as client:
        ids, subs = _seed(app, 60, subscription_ids=2)
        first = client.get("/events")
        second = client.get("/events?page=2")
        with Session(app.state.engine) as session:
            ev = session.get(SubscriptionEvent, ids[-1])
            ev.read_at = ev.created_at
            session.add(ev)
            session.commit()
        unread = client.get("/events?unread=1")
        by_sub = client.get(f"/events?subscription={subs[0]}")
    assert first.text.count("release-title") == 50
    assert "Older" in first.text and "Newer" not in first.text
    assert second.text.count("release-title") == 10
    assert "Newer" in second.text and "Older" not in second.text
    assert "Event number 059" not in unread.text
    assert by_sub.text.count("release-title") == 30
    assert "Event number 001" not in by_sub.text


def test_mark_read_returns_to_filtered_page_and_rejects_external(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "events-read.db"))
    app = create_app()
    with TestClient(app) as client:
        ids, _ = _seed(app, 2)
        token = create_csrf_token(None)
        ok = client.post(
            f"/events/{ids[0]}/read",
            data={"csrf_token": token, "return_to": "/events?unread=1&page=2"},
            follow_redirects=False,
        )
        for bad in ("https://evil.example/", "//evil.example", "/eventsx", "/other"):
            r = client.post(
                f"/events/{ids[1]}/read",
                data={"csrf_token": token, "return_to": bad},
                follow_redirects=False,
            )
            assert r.headers["location"] == f"/events#event-{ids[1]}", bad
    assert ok.status_code == 303
    assert ok.headers["location"] == f"/events?unread=1&page=2#event-{ids[0]}"


def test_events_named_quality_dates_and_delivery_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "events-copy.db"))
    app = create_app()
    with TestClient(app) as client:
        ids, _ = _seed(app, 2)
        with Session(app.state.engine) as session:
            ev = session.get(SubscriptionEvent, ids[0])
            ev.kind = SubscriptionEventKind.UPGRADE_PROPOSAL
            ev.prior_quality_score = [3, 2, 2]
            ev.current_quality_score = [4, 2, 5]
            session.add(ev)
            session.add(NotificationDeliveryAttempt(
                event_id=ids[0], channel=NotificationChannel.EMAIL,
                outcome=DeliveryOutcome.FAILED, error_summary="timeout"))
            session.add(NotificationDeliveryAttempt(
                event_id=ids[0], channel=NotificationChannel.TELEGRAM,
                outcome=DeliveryOutcome.SKIPPED, error_summary="not configured"))
            session.commit()
        page = client.get("/events")
    text = page.text
    assert "3 / 2 / 2" not in text
    assert "Resolution: 1080p → " in text and "4K (2160p)" in text
    assert "Dolby Vision" in text and "quality-changed" in text
    assert "<time datetime=" in text and " UTC</time>" in text
    assert "Failed — timeout, not retried automatically" in text
    assert "Skipped — not configured" in text
    assert "Not attempted" in text
