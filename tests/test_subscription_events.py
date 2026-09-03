from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import SQLModel, Session, select

from skald.db import get_engine, migrate_schema
from skald.indexer.base import ReleaseResult
from skald.models import (
    DownloadedQuality,
    MediaJob,
    MediaSubscription,
    MediaType,
    QualityProfile,
    SubscriptionEvent,
    SubscriptionEventKind,
    SubscriptionRelease,
    TvSubscriptionScope,
)
from skald.services.events import create_release_match, create_upgrade_proposal
from skald.services.events import create_upgrade_proposals
from skald.quality import ObservedQuality
from skald.subscriptions import scan_due_subscriptions, target_keys_for_subscription_release


class ScanIndexer:
    def __init__(self, releases: list[ReleaseResult]) -> None:
        self.releases = releases

    async def search(self, query: str) -> list[ReleaseResult]:
        return self.releases


class RecordingDeliveryService:
    def __init__(self) -> None:
        self.event_ids: list[int] = []

    def deliver_event(self, event_id: int) -> None:
        self.event_ids.append(event_id)


class CommitObservingDeliveryService:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.observed: list[tuple[int, int]] = []

    def deliver_event(self, event_id: int) -> None:
        with Session(self.engine) as observer:
            event = observer.get(SubscriptionEvent, event_id)
            assert event is not None
            release = observer.get(SubscriptionRelease, event.subscription_release_id)
            assert release is not None
            self.observed.append((event.id, release.id))


@pytest.fixture
def session(tmp_path):
    engine = get_engine(str(tmp_path / "subscription-events.db"))
    SQLModel.metadata.create_all(engine)
    migrate_schema(engine)
    with Session(engine) as database_session:
        yield database_session


def _subscription(session: Session, *, tmdb_id: int = 603) -> MediaSubscription:
    subscription = MediaSubscription(
        tmdb_id=tmdb_id,
        type=MediaType.MOVIE,
        title="The Matrix",
        year=1999,
    )
    session.add(subscription)
    session.commit()
    return subscription


def _release(session: Session, subscription: MediaSubscription, fingerprint: str) -> SubscriptionRelease:
    release = SubscriptionRelease(
        subscription_id=subscription.id,
        release_title="The.Matrix.1999.2160p.7.1.HDR10",
        indexer="fake",
        size_bytes=2_000_000_000,
        seeders=9,
        leechers=0,
        download_url=f"magnet:?{fingerprint}",
        fingerprint=fingerprint,
    )
    session.add(release)
    session.commit()
    return release


def test_event_service_deduplicates_release_match_and_strict_upgrade(session):
    subscription = _subscription(session)
    release = _release(session, subscription, "event-service")

    match = create_release_match(session, release, subscription)
    duplicate_match = create_release_match(session, release, subscription)
    proposal = create_upgrade_proposal(
        session,
        subscription_id=subscription.id,
        release_id=release.id,
        media_type=MediaType.MOVIE,
        target_key="movie:tmdb:603",
        title="Upgrade available",
        body="1080p → 2160p",
        prior_score=[3, 2, 2],
        current_score=[4, 3, 3],
    )
    duplicate_proposal = create_upgrade_proposal(
        session,
        subscription_id=subscription.id,
        release_id=release.id,
        media_type=MediaType.MOVIE,
        target_key="movie:tmdb:603",
        title="Upgrade available",
        body="1080p → 2160p",
        prior_score=[3, 2, 2],
        current_score=[4, 3, 3],
    )

    assert match is not None
    assert duplicate_match is None
    assert proposal is not None
    assert duplicate_proposal is None
    assert [event.kind for event in session.exec(select(SubscriptionEvent).order_by(SubscriptionEvent.id))] == [
        SubscriptionEventKind.RELEASE_MATCH,
        SubscriptionEventKind.UPGRADE_PROPOSAL,
    ]


async def test_scan_persists_eligible_events_before_delivery_and_keeps_hard_failures(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _subscription(session)
    subscription.next_check_at = now
    profile = session.exec(
        select(QualityProfile).where(QualityProfile.media_type == MediaType.MOVIE)
    ).one()
    profile.minimum_seeders = 10
    session.add_all([subscription, profile])
    session.commit()
    delivery = RecordingDeliveryService()

    await scan_due_subscriptions(
        session,
        ScanIndexer([
            ReleaseResult("The.Matrix.1999.1080p", "fake", 1, 9, 0, "magnet:?hard-fail"),
        ]),
        interval_seconds=60,
        now=now,
        delivery_service=delivery,
    )

    assert [release.release_title for release in session.exec(select(SubscriptionRelease)).all()] == [
        "The.Matrix.1999.1080p"
    ]
    assert session.exec(select(SubscriptionEvent)).all() == []
    assert delivery.event_ids == []


async def test_scan_creates_release_match_and_strict_upgrade_after_baseline(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _subscription(session)
    subscription.next_check_at = now
    job = MediaJob(
        type=MediaType.MOVIE,
        title="The Matrix",
        year=1999,
        release_title="The.Matrix.1999.1080p",
        qbit_hash="existing",
        category="skald-movie",
    )
    session.add_all([subscription, job])
    session.commit()
    session.add(DownloadedQuality(
        media_type=MediaType.MOVIE,
        target_key="movie:tmdb:603",
        media_job_id=job.id,
        resolution="1080p",
        audio="5.1",
        hdr="hdr",
        score_version="v1",
        quality_score=[3, 2, 2],
    ))
    session.commit()
    delivery = RecordingDeliveryService()

    await scan_due_subscriptions(
        session,
        ScanIndexer([
            ReleaseResult(
                "The.Matrix.1999.2160p.7.1.HDR10", "fake", 2_000_000_000, 9, 0,
                "magnet:?upgrade",
            ),
        ]),
        interval_seconds=60,
        now=now,
        delivery_service=delivery,
    )

    events = session.exec(select(SubscriptionEvent).order_by(SubscriptionEvent.kind)).all()
    assert [event.kind for event in events] == [
        SubscriptionEventKind.RELEASE_MATCH,
        SubscriptionEventKind.UPGRADE_PROPOSAL,
    ]
    assert delivery.event_ids == [event.id for event in events]
    assert session.exec(select(MediaJob)).all() == [job]


async def test_delivery_observer_sees_committed_event_and_discovery_from_a_separate_session(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _subscription(session)
    subscription.next_check_at = now
    session.add(subscription)
    session.commit()
    observer = CommitObservingDeliveryService(session.get_bind())

    await scan_due_subscriptions(
        session,
        ScanIndexer([ReleaseResult("The.Matrix.1999.1080p", "fake", 1, 9, 0, "magnet:?observe")]),
        interval_seconds=60,
        now=now,
        delivery_service=observer,
    )

    assert observer.observed == [(event.id, event.subscription_release_id) for event in session.exec(
        select(SubscriptionEvent)
    ).all()]


def test_upgrade_proposals_skip_equal_or_worse_baselines(session):
    subscription = _subscription(session)
    release = _release(session, subscription, "equal-worse")
    job = MediaJob(
        type=MediaType.MOVIE,
        title="The Matrix",
        release_title="The.Matrix.1999.2160p",
        qbit_hash="baseline",
        category="skald-movie",
    )
    session.add(job)
    session.commit()
    session.add(DownloadedQuality(
        media_type=MediaType.MOVIE,
        target_key="movie:tmdb:603",
        media_job_id=job.id,
        resolution="2160p",
        audio="atmos",
        hdr="dolby_vision",
        score_version="v1",
        quality_score=[4, 4, 5],
    ))
    session.commit()

    equal = create_upgrade_proposals(
        session,
        release,
        subscription,
        ObservedQuality("2160p", "atmos", "dolby_vision", None),
        ["movie:tmdb:603"],
    )
    worse = create_upgrade_proposals(
        session,
        release,
        subscription,
        ObservedQuality("1080p", "5.1", "hdr", None),
        ["movie:tmdb:603"],
    )

    assert equal == []
    assert worse == []


def test_tv_target_keys_intersect_episodes_and_packs_with_matching_scopes(session):
    tv = MediaSubscription(tmdb_id=1396, type=MediaType.TV, title="Breaking Bad")
    season = TvSubscriptionScope(
        subscription_id=1, tmdb_series_id=1396, tmdb_season_id=3577, season_number=2
    )
    episode_two = TvSubscriptionScope(
        subscription_id=1,
        tmdb_series_id=1396,
        tmdb_season_id=3577,
        tmdb_episode_id=62084,
        season_number=2,
        episode_number=2,
    )

    assert target_keys_for_subscription_release(
        tv, {"season": 2, "episode_set": (4, 2, 4)}, [episode_two]
    ) == [
        "tv:tmdb:1396:season:2:episode:2",
    ]
    assert target_keys_for_subscription_release(
        tv, {"season": 2, "episode_set": ()}, [season]
    ) == [
        "tv:tmdb:1396:season:2:pack"
    ]
    assert target_keys_for_subscription_release(tv, {"season": 2, "episode_set": ()}, [episode_two]) == []
    assert target_keys_for_subscription_release(tv, {"season": None, "episode_set": ()}, [season]) == []
    assert target_keys_for_subscription_release(tv, {"season": 2, "episode_set": "bad"}, [season]) == []
    assert target_keys_for_subscription_release(tv, {"season": 2, "episode_set": (0,)}, [season]) == []


def test_multi_episode_upgrade_proposals_are_independent_and_deduplicated(session):
    subscription = MediaSubscription(tmdb_id=1396, type=MediaType.TV, title="Breaking Bad")
    session.add(subscription)
    session.commit()
    release = SubscriptionRelease(
        subscription_id=subscription.id,
        release_title="Breaking.Bad.S02E02-E03.2160p",
        indexer="fake",
        size_bytes=1,
        seeders=9,
        leechers=0,
        download_url="magnet:?multi",
        fingerprint="multi-proposal",
    )
    job = MediaJob(
        type=MediaType.TV,
        title="Breaking Bad",
        release_title="Breaking.Bad.S02E02.1080p",
        qbit_hash="baseline-tv",
        category="skald-tv",
    )
    session.add_all([release, job])
    session.commit()
    for episode in (2, 3):
        session.add(DownloadedQuality(
            media_type=MediaType.TV,
            target_key=f"tv:tmdb:1396:season:2:episode:{episode}",
            media_job_id=job.id,
            resolution="1080p",
            audio="5.1",
            hdr="hdr",
            score_version="v1",
            quality_score=[3, 2, 2],
        ))
    session.commit()

    target_keys = target_keys_for_subscription_release(
        subscription,
        {"season": 2, "episode_set": (2, 3)},
        [
            TvSubscriptionScope(
                subscription_id=subscription.id,
                tmdb_series_id=1396,
                tmdb_season_id=3577,
                tmdb_episode_id=62084,
                season_number=2,
                episode_number=2,
            ),
            TvSubscriptionScope(
                subscription_id=subscription.id,
                tmdb_series_id=1396,
                tmdb_season_id=3577,
                tmdb_episode_id=62085,
                season_number=2,
                episode_number=3,
            ),
        ],
    )
    first = create_upgrade_proposals(
        session, release, subscription, ObservedQuality("2160p", "7.1", "hdr10", None), target_keys
    )
    second = create_upgrade_proposals(
        session, release, subscription, ObservedQuality("2160p", "7.1", "hdr10", None), target_keys
    )

    assert len(first) == 2
    assert second == []


async def test_scan_proposes_tv_upgrades_only_for_persisted_matching_scope_episodes(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = MediaSubscription(
        tmdb_id=1396, type=MediaType.TV, title="Breaking Bad", next_check_at=now
    )
    job = MediaJob(
        type=MediaType.TV,
        title="Breaking Bad",
        season=2,
        episode=2,
        release_title="Breaking.Bad.S02E02.1080p",
        qbit_hash="baseline-tv-scope",
        category="skald-tv",
    )
    session.add_all([subscription, job])
    session.commit()
    session.add(TvSubscriptionScope(
        subscription_id=subscription.id,
        tmdb_series_id=1396,
        tmdb_season_id=3577,
        tmdb_episode_id=62084,
        season_number=2,
        episode_number=2,
    ))
    for episode in (2, 3):
        session.add(DownloadedQuality(
            media_type=MediaType.TV,
            target_key=f"tv:tmdb:1396:season:2:episode:{episode}",
            media_job_id=job.id,
            resolution="1080p",
            audio="5.1",
            hdr="hdr",
            score_version="v1",
            quality_score=[3, 2, 2],
        ))
    session.commit()

    await scan_due_subscriptions(
        session,
        ScanIndexer([
            ReleaseResult(
                "Breaking.Bad.S02E02-E03.2160p.7.1.HDR10",
                "fake",
                2_000_000_000,
                9,
                0,
                "magnet:?scoped-upgrade",
            ),
        ]),
        interval_seconds=60,
        now=now,
    )

    proposals = session.exec(select(SubscriptionEvent).where(
        SubscriptionEvent.kind == SubscriptionEventKind.UPGRADE_PROPOSAL
    )).all()
    assert [proposal.target_key for proposal in proposals] == [
        "tv:tmdb:1396:season:2:episode:2"
    ]


async def test_repeated_scans_do_not_duplicate_events(session):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _subscription(session)
    subscription.next_check_at = now
    session.add(subscription)
    session.commit()
    indexer = ScanIndexer([ReleaseResult("The.Matrix.1999.1080p", "fake", 1, 9, 0, "magnet:?repeat")])

    await scan_due_subscriptions(session, indexer, interval_seconds=60, now=now)
    subscription.next_check_at = now + timedelta(seconds=60)
    session.add(subscription)
    session.commit()
    await scan_due_subscriptions(session, indexer, interval_seconds=60, now=now + timedelta(seconds=60))

    assert len(session.exec(select(SubscriptionEvent)).all()) == 1
