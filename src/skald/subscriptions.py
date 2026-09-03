import hashlib
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlmodel import Session, select

from skald.config import Settings
from skald.indexer.base import IndexerClient, ReleaseResult
from skald.models import (
    MediaSubscription,
    MediaType,
    QualityProfile,
    SubscriptionRelease,
    SubscriptionReleaseScope,
    TvSubscriptionScope,
)
from skald.episodes import serialize_episode_set
from skald.parser import parse_release
from skald.quality import best_matching_release, default_quality_profile
from skald.services.grab import MediaJobCreationError, TorrentAdder, create_media_job

Clock = datetime | Callable[[], datetime]
ProfileProvider = Callable[[], QualityProfile | None]
_AUTO_GRAB_FAILURE_PREFIX = "Automatic grab failed: "


def release_fingerprint(subscription_id: int, release: ReleaseResult) -> str:
    value = "\x1f".join(
        (
            str(subscription_id),
            release.indexer,
            release.download_url,
            release.title,
            str(release.size_bytes),
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


def subscription_query(subscription: MediaSubscription) -> str:
    title = subscription.original_title or subscription.title
    return f"{title} {subscription.year}" if subscription.year else title


def tv_scope_matches_release(scope: TvSubscriptionScope, release_title: str) -> bool:
    """Return whether a parsed TV release is within one persisted TV target."""
    parsed = parse_release(release_title)
    if parsed["media_type"] != MediaType.TV.value:
        return False
    if scope.includes_future_content:
        return True
    if parsed["season"] != scope.season_number:
        return False
    # A selected season includes its individual episodes and season packs.
    if scope.episode_number is None:
        return True
    return scope.episode_number in parsed["episode_set"]


def matching_tv_subscription_scopes(
    session: Session, subscription: MediaSubscription, release_title: str
) -> list[TvSubscriptionScope]:
    """Load the persisted TV targets a release satisfies, in stable order."""
    if subscription.type != MediaType.TV:
        return []
    scopes = session.exec(
        select(TvSubscriptionScope)
        .where(TvSubscriptionScope.subscription_id == subscription.id)
        .order_by(TvSubscriptionScope.id)
    ).all()
    return [scope for scope in scopes if tv_scope_matches_release(scope, release_title)]


def tv_target_episode_numbers(
    release_title: str, scopes: list[TvSubscriptionScope]
) -> tuple[int, ...]:
    """Return the requested episode numbers this release can safely download.

    A series or season scope requires every explicitly named episode in a
    release. An episode scope contributes only its own coordinate. A season
    pack without episode coordinates remains a notification: without target
    numbers the selective grab service cannot safely resume it.
    """
    parsed = parse_release(release_title)
    season = parsed["season"]
    release_episodes = set(parsed["episode_set"])
    if parsed["media_type"] != MediaType.TV.value or season is None or not release_episodes:
        return ()

    targets: set[int] = set()
    for scope in scopes:
        if scope.includes_future_content or scope.episode_number is None:
            targets.update(release_episodes)
        elif scope.season_number == season and scope.episode_number in release_episodes:
            targets.add(scope.episode_number)
    return tuple(sorted(targets))


def _persist_release_scope_targets(
    session: Session, release: SubscriptionRelease, scopes: list[TvSubscriptionScope]
) -> None:
    """Attach each matching TV scope once, including on retrying old releases."""
    existing_scope_ids = set(session.exec(
        select(SubscriptionReleaseScope.tv_subscription_scope_id).where(
            SubscriptionReleaseScope.subscription_release_id == release.id
        )
    ).all())
    for scope in scopes:
        if scope.id not in existing_scope_ids:
            session.add(SubscriptionReleaseScope(
                subscription_release_id=release.id,
                tv_subscription_scope_id=scope.id,
            ))


def _published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None


async def scan_due_subscriptions(
    session: Session,
    indexer: IndexerClient,
    *,
    qbit: TorrentAdder | None = None,
    settings: Settings | None = None,
    profile_provider: ProfileProvider | None = None,
    interval_seconds: int,
    now: Clock,
) -> None:
    scan_started_at = now() if callable(now) else now
    due_subscriptions = session.exec(
        select(MediaSubscription)
        .where(MediaSubscription.is_active.is_(True))
        .where(MediaSubscription.next_check_at <= scan_started_at)
        .order_by(MediaSubscription.id)
    ).all()

    for subscription in due_subscriptions:
        subscription_id = subscription.id
        try:
            matching_results: list[ReleaseResult] = []
            newly_discovered_results: list[ReleaseResult] = []
            for release in await indexer.search(subscription_query(subscription)):
                parsed = parse_release(release.title)
                if parsed["media_type"] != subscription.type.value:
                    continue
                scopes: list[TvSubscriptionScope] = []
                if subscription.type == MediaType.TV:
                    scopes = matching_tv_subscription_scopes(session, subscription, release.title)
                    # TV releases are notifications only when they satisfy a
                    # configured series, season, or episode target.
                    if not scopes:
                        continue
                matching_results.append(release)
                fingerprint = release_fingerprint(subscription.id, release)
                stored_release = session.exec(
                    select(SubscriptionRelease).where(
                        SubscriptionRelease.fingerprint == fingerprint
                    )
                ).first()
                if stored_release is None:
                    stored_release = SubscriptionRelease(
                        subscription_id=subscription.id,
                        release_title=release.title,
                        indexer=release.indexer,
                        size_bytes=release.size_bytes,
                        seeders=release.seeders,
                        leechers=release.leechers,
                        download_url=release.download_url,
                        published_at=_published_at(release.published_at),
                        fingerprint=fingerprint,
                    )
                    session.add(stored_release)
                    session.flush()
                    newly_discovered_results.append(release)
                if scopes:
                    _persist_release_scope_targets(session, stored_release, scopes)
            # Persist every discovery before any external qBittorrent effect.
            session.commit()

            durable_subscription = session.get(
                MediaSubscription, subscription_id, populate_existing=True
            )
            if durable_subscription is None:
                continue
            candidates = newly_discovered_results
            if durable_subscription.last_error and durable_subscription.last_error.startswith(
                _AUTO_GRAB_FAILURE_PREFIX
            ):
                # A failed eligible discovery is retryable even though it is
                # no longer new on the next indexer response.
                candidates = matching_results
            if (
                durable_subscription.auto_download
                and durable_subscription.auto_grabbed_release_id is None
                and qbit is not None
                and settings is not None
            ):
                if durable_subscription.type == MediaType.TV:
                    # Do not let an unaddressable season-pack notification
                    # outrank a lower-seeded release whose file targets are
                    # known. The latter is the only safe selective grab.
                    candidates = [
                        release
                        for release in candidates
                        if tv_target_episode_numbers(
                            release.title,
                            matching_tv_subscription_scopes(
                                session, durable_subscription, release.title
                            ),
                        )
                    ]
                profile = (
                    profile_provider() if profile_provider is not None else session.get(QualityProfile, 1)
                ) or default_quality_profile()
                selected_release = best_matching_release(profile, candidates)
                if selected_release is not None:
                    created_job = False
                    selected_row = session.exec(
                        select(SubscriptionRelease).where(
                            SubscriptionRelease.fingerprint
                            == release_fingerprint(subscription_id, selected_release)
                        )
                    ).one()
                    if durable_subscription.type == MediaType.MOVIE:
                        create_media_job(
                            session,
                            qbit,
                            selected_release,
                            media_type=MediaType.MOVIE,
                            title=durable_subscription.title,
                            year=durable_subscription.year,
                            settings=settings,
                        )
                        created_job = True
                    else:
                        scope_ids = session.exec(
                            select(SubscriptionReleaseScope.tv_subscription_scope_id).where(
                                SubscriptionReleaseScope.subscription_release_id == selected_row.id
                            )
                        ).all()
                        selected_scopes = session.exec(
                            select(TvSubscriptionScope)
                            .where(TvSubscriptionScope.id.in_(scope_ids))
                            .order_by(TvSubscriptionScope.id)
                        ).all()
                        target_episodes = tv_target_episode_numbers(
                            selected_release.title, selected_scopes
                        )
                        parsed = parse_release(selected_release.title)
                        if target_episodes:
                            create_media_job(
                                session,
                                qbit,
                                selected_release,
                                media_type=MediaType.TV,
                                title=durable_subscription.title,
                                season=parsed["season"],
                                episode=target_episodes[0],
                                episode_set=serialize_episode_set(target_episodes),
                                target_episode_numbers=target_episodes,
                                settings=settings,
                            )
                            created_job = True
                    # The shared service persists only after qBittorrent
                    # succeeds; mark this subscription consumed afterwards.
                    if created_job:
                        durable_subscription.auto_grabbed_release_id = selected_row.id
            completed_at = now() if callable(now) else now
            durable_subscription.last_checked_at = completed_at
            durable_subscription.last_error = None
            durable_subscription.next_check_at = completed_at + timedelta(seconds=interval_seconds)
            session.add(durable_subscription)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - isolate each durable subscription scan
            session.rollback()
            failed_subscription = session.get(
                MediaSubscription, subscription_id, populate_existing=True
            )
            if failed_subscription is None:
                continue
            failed_subscription.last_error = (
                _auto_grab_error_detail(exc)
                if isinstance(exc, MediaJobCreationError)
                else _scan_error_detail(exc)
            )
            completed_at = now() if callable(now) else now
            failed_subscription.next_check_at = completed_at + timedelta(seconds=interval_seconds)
            session.add(failed_subscription)
            session.commit()


def _scan_error_detail(exc: Exception) -> str:
    """Return a safe, bounded scan failure summary for durable storage."""
    category = type(exc).__name__
    safe_category = "".join(char for char in category if char.isalnum() or char == "_")
    message = " ".join(str(exc).split())
    if re.search(r"https?://|\bapi[\s_-]*key\b", message, re.IGNORECASE):
        return f"{safe_category[:64] or 'Error'}: subscription scan failed"
    return message[:200] or f"{safe_category[:64] or 'Error'}: subscription scan failed"


def _auto_grab_error_detail(exc: Exception) -> str:
    """Return a safe, bounded, retry-identifying auto-grab error."""
    return (_AUTO_GRAB_FAILURE_PREFIX + _scan_error_detail(exc))[:200]
