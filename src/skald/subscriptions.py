import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import logging

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from skald.config import Settings
from skald.indexer.base import IndexerClient, ReleaseResult
from skald.models import (
    DownloadedQuality,
    MediaJob,
    MediaSubscription,
    MediaType,
    QualityProfile,
    SubscriptionRelease,
    SubscriptionReleaseScope,
    TvSubscriptionScope,
)
from skald.episodes import deserialize_episode_set, serialize_episode_set
from skald.parser import parse_release
from skald.quality import QualityCandidate, QualityProfileService
from skald.routes.quality import get_or_create_profile
from skald.services.events import create_release_match, create_upgrade_proposals
from skald.services.grab import MediaJobCreationError, TorrentAdder, create_media_job
from skald.services.notifications import NotificationDeliveryService

Clock = datetime | Callable[[], datetime]
ProfileProvider = Callable[[], QualityProfile | None]
_AUTO_GRAB_FAILURE_PREFIX = "Automatic grab failed: "
logger = logging.getLogger(__name__)


def release_fingerprint(subscription_id: int, release: ReleaseResult) -> str:
    # Jackett download URLs are re-encrypted on every search, so prefer a
    # stable identity: info-hash, then indexer guid, then the URL.
    identity = (
        (release.info_hash or "").strip().lower()
        or (release.guid or "").strip()
        or release.download_url
    )
    value = "\x1f".join(
        (
            str(subscription_id),
            release.indexer,
            identity,
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


def persisted_tv_subscription_scopes(
    session: Session, release: SubscriptionRelease
) -> list[TvSubscriptionScope]:
    """Load the durable TV targets attached to one discovered release."""
    return session.exec(
        select(TvSubscriptionScope)
        .join(
            SubscriptionReleaseScope,
            SubscriptionReleaseScope.tv_subscription_scope_id == TvSubscriptionScope.id,
        )
        .where(SubscriptionReleaseScope.subscription_release_id == release.id)
        .order_by(TvSubscriptionScope.id)
    ).all()


def _published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None


def target_keys_for_subscription_release(
    subscription: MediaSubscription,
    parsed: Mapping[str, object],
    matching_scopes: list[TvSubscriptionScope],
) -> list[str]:
    """Return canonical baseline targets covered by durable release scopes."""
    if subscription.type is MediaType.MOVIE:
        return [f"movie:tmdb:{subscription.tmdb_id}"]
    if subscription.type is not MediaType.TV:
        return []
    season = parsed.get("season")
    if not isinstance(season, int) or isinstance(season, bool):
        return []
    raw_episodes = parsed.get("episode_set")
    if not isinstance(raw_episodes, (list, tuple, set)):
        return []
    episodes = {
        episode
        for episode in raw_episodes
        if isinstance(episode, int) and not isinstance(episode, bool) and episode > 0
    }
    if episodes:
        targets: set[int] = set()
        for scope in matching_scopes:
            if scope.includes_future_content:
                targets.update(episodes)
            elif scope.season_number == season:
                if scope.episode_number is None:
                    targets.update(episodes)
                elif scope.episode_number in episodes:
                    targets.add(scope.episode_number)
        return [
            f"tv:tmdb:{subscription.tmdb_id}:season:{season}:episode:{episode}"
            for episode in sorted(targets)
        ]
    if raw_episodes:
        return []
    if any(
        scope.includes_future_content
        or (scope.season_number == season and scope.episode_number is None)
        for scope in matching_scopes
    ):
        return [f"tv:tmdb:{subscription.tmdb_id}:season:{season}:pack"]
    return []


async def scan_due_subscriptions(
    session: Session,
    indexer: IndexerClient,
    *,
    qbit: TorrentAdder | None = None,
    settings: Settings | None = None,
    profile_provider: ProfileProvider | None = None,
    delivery_service: NotificationDeliveryService | None = None,
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
            retry_auto_grab = bool(
                subscription.last_error
                and subscription.last_error.startswith(_AUTO_GRAB_FAILURE_PREFIX)
            )
            matching_results: list[ReleaseResult] = []
            newly_discovered_results: list[ReleaseResult] = []
            candidates_by_fingerprint: dict[str, QualityCandidate] = {}
            created_event_ids: list[int] = []
            profile = profile_provider() if profile_provider is not None else None
            if profile is None or profile.media_type is not subscription.type:
                profile = get_or_create_profile(session, subscription.type)
            profile_service = QualityProfileService()
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
                observed = profile_service.observed_from_parsed(parsed, release.size_bytes)
                candidates_by_fingerprint[fingerprint] = QualityCandidate(
                    release=release, fingerprint=fingerprint, observed=observed
                )
                stored_release = session.exec(
                    select(SubscriptionRelease).where(
                        SubscriptionRelease.fingerprint == fingerprint
                    )
                ).first()
                newly_inserted = stored_release is None
                if stored_release is None:
                    candidate_release = SubscriptionRelease(
                        subscription_id=subscription.id,
                        release_title=release.title,
                        indexer=release.indexer,
                        size_bytes=release.size_bytes,
                        seeders=release.seeders,
                        leechers=release.leechers,
                        download_url=release.download_url,
                        published_at=_published_at(release.published_at),
                        fingerprint=fingerprint,
                        resolution=observed.resolution,
                        audio=observed.audio,
                        hdr=observed.hdr,
                    )
                    try:
                        with session.begin_nested():
                            session.add(candidate_release)
                            session.flush()
                    except IntegrityError:
                        stored_release = session.exec(
                            select(SubscriptionRelease).where(
                                SubscriptionRelease.fingerprint == fingerprint
                            )
                        ).first()
                        if stored_release is None:
                            raise
                        newly_inserted = False
                    else:
                        stored_release = candidate_release
                        newly_discovered_results.append(release)
                if scopes:
                    _persist_release_scope_targets(session, stored_release, scopes)
                    session.flush()
                if newly_inserted and profile_service.eligible(
                    profile, observed, title=release.title, seeders=release.seeders
                ):
                    event = create_release_match(session, stored_release, subscription)
                    if event is not None and event.id is not None:
                        created_event_ids.append(event.id)
                    for proposal in create_upgrade_proposals(
                        session,
                        stored_release,
                        subscription,
                        observed,
                        target_keys_for_subscription_release(
                            subscription,
                            parsed,
                            persisted_tv_subscription_scopes(session, stored_release),
                        ),
                    ):
                        if proposal.id is not None:
                            created_event_ids.append(proposal.id)
            # Persist every discovery and event before any delivery or qBittorrent effect.
            completed_at = now() if callable(now) else now
            subscription.last_checked_at = completed_at
            subscription.last_error = None
            subscription.next_check_at = completed_at + timedelta(seconds=interval_seconds)
            session.add(subscription)
            session.commit()

            if delivery_service is not None:
                for event_id in created_event_ids:
                    try:
                        await asyncio.to_thread(delivery_service.deliver_event, event_id)
                    except Exception:  # noqa: BLE001 - delivery cannot affect committed discovery
                        logger.exception("event delivery invocation failed for event %s", event_id)

            durable_subscription = session.get(
                MediaSubscription, subscription_id, populate_existing=True
            )
            if durable_subscription is None:
                continue
            candidates = newly_discovered_results
            if retry_auto_grab:
                # A failed eligible discovery is retryable even though it is
                # no longer new on the next indexer response.
                candidates = matching_results
            if (
                durable_subscription.auto_download
                and (
                    durable_subscription.type == MediaType.TV
                    or durable_subscription.auto_grabbed_release_id is None
                )
                and qbit is not None
                and settings is not None
            ):
                if durable_subscription.type == MediaType.TV:
                    _auto_grab_tv_episodes(
                        session,
                        qbit,
                        settings,
                        durable_subscription,
                        profile,
                        profile_service,
                        candidates,
                        candidates_by_fingerprint,
                    )
                else:
                    ranked_candidates = profile_service.rank(
                        profile,
                        (
                            candidates_by_fingerprint[release_fingerprint(subscription_id, release)]
                            for release in candidates
                            if release_fingerprint(subscription_id, release)
                            in candidates_by_fingerprint
                        ),
                    )
                    if ranked_candidates:
                        selected_release = ranked_candidates[0].release
                        selected_row = session.exec(
                            select(SubscriptionRelease).where(
                                SubscriptionRelease.fingerprint
                                == release_fingerprint(subscription_id, selected_release)
                            )
                        ).one()
                        create_media_job(
                            session,
                            qbit,
                            selected_release,
                            media_type=MediaType.MOVIE,
                            title=durable_subscription.title,
                            year=durable_subscription.year,
                            source_subscription_id=durable_subscription.id,
                            source_subscription_release_id=selected_row.id,
                            settings=settings,
                        )
                        # The shared service persists only after qBittorrent
                        # succeeds; mark this subscription consumed afterwards.
                        durable_subscription.auto_grabbed_release_id = selected_row.id
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


def _auto_grab_tv_episodes(
    session: Session,
    qbit: TorrentAdder,
    settings: Settings,
    subscription: MediaSubscription,
    profile: QualityProfile,
    profile_service: QualityProfileService,
    candidates: list[ReleaseResult],
    candidates_by_fingerprint: dict[str, QualityCandidate],
) -> None:
    """Grab every addressable target episode that has no job yet, best rank first."""
    subscription_id = subscription.id
    covered: set[tuple[int | None, int]] = set()
    for job in session.exec(
        select(MediaJob).where(MediaJob.source_subscription_id == subscription_id)
    ).all():
        job_episodes = deserialize_episode_set(job.episode_set) or (
            (job.episode,) if job.episode is not None else ()
        )
        covered.update((job.season, number) for number in job_episodes)
    # Library files outlive removed job rows, so baselines also count as covered.
    key_prefix = f"tv:tmdb:{subscription.tmdb_id}:season:"
    for target_key in session.exec(
        select(DownloadedQuality.target_key)
        .where(DownloadedQuality.media_type == MediaType.TV)
        .where(DownloadedQuality.target_key.startswith(key_prefix))
    ).all():
        match = re.fullmatch(re.escape(key_prefix) + r"(\d+):episode:(\d+)", target_key)
        if match:
            covered.add((int(match.group(1)), int(match.group(2))))

    addressable: list[tuple[ReleaseResult, list[int]]] = []
    for release in candidates:
        targets = tv_target_episode_numbers(
            release.title,
            matching_tv_subscription_scopes(session, subscription, release.title),
        )
        if targets and release_fingerprint(subscription_id, release) in candidates_by_fingerprint:
            addressable.append((release, list(targets)))
    targets_by_fingerprint = {
        release_fingerprint(subscription_id, release): targets for release, targets in addressable
    }
    ranked = profile_service.rank(
        profile,
        (candidates_by_fingerprint[fp] for fp in targets_by_fingerprint),
    )
    for candidate in ranked:
        release = candidate.release
        season = parse_release(release.title)["season"]
        remaining = [
            number
            for number in targets_by_fingerprint[candidate.fingerprint]
            if (season, number) not in covered
        ]
        if not remaining:
            continue
        row = session.exec(
            select(SubscriptionRelease).where(SubscriptionRelease.fingerprint == candidate.fingerprint)
        ).one()
        # create_media_job commits, so earlier grabs survive a later failure.
        create_media_job(
            session,
            qbit,
            release,
            media_type=MediaType.TV,
            title=subscription.title,
            season=season,
            episode=remaining[0],
            episode_set=serialize_episode_set(remaining),
            target_episode_numbers=remaining,
            source_subscription_id=subscription_id,
            source_subscription_release_id=row.id,
            settings=settings,
        )
        covered.update((season, number) for number in remaining)
        # Display only; never used as a gate for TV.
        subscription.auto_grabbed_release_id = row.id
        session.add(subscription)
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
