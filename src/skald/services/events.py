"""Transactional creation of durable subscription-event snapshots."""

from collections.abc import Iterable

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from skald.models import (
    DownloadedQuality,
    MediaSubscription,
    MediaType,
    SubscriptionEvent,
    SubscriptionEventKind,
    SubscriptionRelease,
)
from skald.quality import ObservedQuality, QualityProfileService


def create_release_match(
    session: Session, release: SubscriptionRelease, subscription: MediaSubscription
) -> SubscriptionEvent | None:
    """Create one event keyed by release ID, or return None when it already exists."""
    return _create_event(
        session,
        dedupe_key=f"release:{release.id}",
        subscription_id=subscription.id,
        release_id=release.id,
        media_type=subscription.type,
        target_key=None,
        kind=SubscriptionEventKind.RELEASE_MATCH,
        title=f"New {subscription.type.value} release: {subscription.title}",
        body=f"{release.release_title} ({release.seeders} seeders)",
        prior_score=None,
        current_score=None,
    )


def create_upgrade_proposal(
    session: Session,
    *,
    subscription_id: int,
    release_id: int,
    media_type: MediaType,
    target_key: str,
    title: str,
    body: str,
    prior_score: list[int],
    current_score: list[int],
) -> SubscriptionEvent | None:
    """Create one proposal per target/release pair, or return None when present."""
    return _create_event(
        session,
        dedupe_key=f"upgrade:{target_key}:{release_id}",
        subscription_id=subscription_id,
        release_id=release_id,
        media_type=media_type,
        target_key=target_key,
        kind=SubscriptionEventKind.UPGRADE_PROPOSAL,
        title=title,
        body=body,
        prior_score=_score_snapshot(prior_score),
        current_score=_score_snapshot(current_score),
    )


def create_upgrade_proposals(
    session: Session,
    release: SubscriptionRelease,
    subscription: MediaSubscription,
    observed: ObservedQuality,
    target_keys: Iterable[str],
) -> list[SubscriptionEvent]:
    """Create proposals only for targets whose existing baseline is strictly worse."""
    score = list(QualityProfileService().fixed_score(observed))
    events: list[SubscriptionEvent] = []
    for target_key in sorted(set(target_keys)):
        baseline = session.exec(
            select(DownloadedQuality).where(
                DownloadedQuality.media_type == subscription.type,
                DownloadedQuality.target_key == target_key,
            )
        ).first()
        if baseline is None or not _is_strictly_better(score, baseline.quality_score):
            continue
        event = create_upgrade_proposal(
            session,
            subscription_id=subscription.id,
            release_id=release.id,
            media_type=subscription.type,
            target_key=target_key,
            title=f"Upgrade available: {subscription.title}",
            body=(
                f"{release.release_title} ({release.seeders} seeders) "
                f"is better than the current {target_key} download."
            ),
            prior_score=list(baseline.quality_score),
            current_score=score,
        )
        if event is not None:
            events.append(event)
    return events


def _create_event(
    session: Session,
    *,
    dedupe_key: str,
    subscription_id: int,
    release_id: int | None,
    media_type: MediaType,
    target_key: str | None,
    kind: SubscriptionEventKind,
    title: str,
    body: str,
    prior_score: list[int] | None,
    current_score: list[int] | None,
) -> SubscriptionEvent | None:
    event = SubscriptionEvent(
        subscription_id=subscription_id,
        subscription_release_id=release_id,
        media_type=media_type,
        target_key=target_key,
        kind=kind,
        dedupe_key=dedupe_key,
        title=title[:200],
        body=body[:1000],
        prior_quality_score=prior_score,
        current_quality_score=current_score,
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        existing = session.exec(
            select(SubscriptionEvent).where(SubscriptionEvent.dedupe_key == dedupe_key)
        ).first()
        if existing is not None:
            return None
        raise
    return event


def _score_snapshot(score: list[int]) -> list[int]:
    if len(score) != 3 or any(not isinstance(rank, int) or isinstance(rank, bool) for rank in score):
        raise ValueError("Quality score must contain three integer ranks")
    return list(score)


def _is_strictly_better(current: list[int], prior: object) -> bool:
    if not isinstance(prior, list):
        return False
    try:
        return tuple(current) > tuple(_score_snapshot(prior))
    except ValueError:
        return False
