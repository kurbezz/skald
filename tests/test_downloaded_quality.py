from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from skald.models import (
    DownloadedQuality,
    JobStatus,
    MediaJob,
    MediaSubscription,
    MediaType,
    OrganizationMode,
    QualityProfile,
    SubscriptionRelease,
)
from skald.services.downloaded_quality import (
    record_organized_quality,
    target_keys_for_job,
)


def make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


def sourced_job(
    session, *, media_type, title, release_title, season=None, episode_set=None,
    organization_mode=OrganizationMode.SCALAR, tmdb_id=603,
):
    subscription = MediaSubscription(tmdb_id=tmdb_id, type=media_type, title=title)
    session.add(subscription)
    session.commit()
    release = SubscriptionRelease(
        subscription_id=subscription.id,
        release_title=release_title,
        indexer="fake",
        size_bytes=0,
        seeders=9,
        leechers=0,
        download_url=f"magnet:?{release_title}",
        fingerprint=f"fingerprint:{release_title}",
    )
    session.add(release)
    session.commit()
    job = MediaJob(
        type=media_type,
        title=title,
        season=season,
        episode_set=episode_set,
        release_title=release_title,
        qbit_hash=f"hash:{release_title}",
        category="skald-tv" if media_type is MediaType.TV else "skald-movie",
        status=JobStatus.ORGANIZED,
        organization_mode=organization_mode,
        source_subscription_id=subscription.id,
        source_subscription_release_id=release.id,
    )
    session.add(job)
    session.commit()
    return subscription, release, job


def test_target_keys_require_organized_source_and_cover_movie_episode_and_pack():
    engine = make_engine()
    with Session(engine) as session:
        _, _, movie = sourced_job(
            session,
            media_type=MediaType.MOVIE,
            title="The Matrix",
            release_title="The.Matrix.1999.2160p",
        )
        _, _, episodes = sourced_job(
            session,
            media_type=MediaType.TV,
            title="Show",
            release_title="Show.S02E03-E04.1080p",
            season=2,
            episode_set="[3,4]",
        )
        _, _, pack = sourced_job(
            session,
            media_type=MediaType.TV,
            title="Other Show",
            release_title="Other.Show.S01.1080p",
            season=1,
            organization_mode=OrganizationMode.PACK,
            tmdb_id=604,
        )
        manual = MediaJob(
            type=MediaType.MOVIE,
            title="Manual",
            release_title="Manual.2160p",
            qbit_hash="manual",
            category="skald-movie",
            status=JobStatus.ORGANIZED,
        )
        session.add(manual)
        session.commit()

        assert target_keys_for_job(session, movie) == ["movie:tmdb:603"]
        assert target_keys_for_job(session, episodes) == [
            "tv:tmdb:603:season:2:episode:3",
            "tv:tmdb:603:season:2:episode:4",
        ]
        assert target_keys_for_job(session, pack) == ["tv:tmdb:604:season:1:pack"]
        assert target_keys_for_job(session, manual) == []
        movie.status = JobStatus.COMPLETED
        assert target_keys_for_job(session, movie) == []


def test_target_keys_use_only_the_durable_downloaded_episode_subset_and_validate_it():
    engine = make_engine()
    with Session(engine) as session:
        _, _, job = sourced_job(
            session,
            media_type=MediaType.TV,
            title="Show",
            release_title="Show.S02E03-E04.1080p",
            season=2,
            episode_set="[3]",
        )
        _, _, invalid_subset = sourced_job(
            session,
            media_type=MediaType.TV,
            title="Other Show",
            release_title="Other.Show.S02E03-E04.1080p",
            season=2,
            episode_set="[3,5]",
            tmdb_id=604,
        )
        _, _, scalar_pack = sourced_job(
            session,
            media_type=MediaType.TV,
            title="Scalar Show",
            release_title="Scalar.Show.S01.1080p",
            season=1,
            tmdb_id=605,
        )
        mismatched = session.get(MediaSubscription, job.source_subscription_id)
        mismatched.type = MediaType.MOVIE
        session.add(mismatched)
        session.commit()

        assert target_keys_for_job(session, job) == []
        mismatched.type = MediaType.TV
        session.add(mismatched)
        session.commit()
        assert target_keys_for_job(session, job) == ["tv:tmdb:603:season:2:episode:3"]
        assert target_keys_for_job(session, invalid_subset) == []
        assert target_keys_for_job(session, scalar_pack) == []


def test_record_organized_quality_replaces_only_a_strictly_better_baseline():
    engine = make_engine()
    with Session(engine) as session:
        _, _, better_job = sourced_job(
            session,
            media_type=MediaType.MOVIE,
            title="The Matrix",
            release_title="The.Matrix.1999.2160p.Atmos.DV",
        )
        worse_release = SubscriptionRelease(
            subscription_id=better_job.source_subscription_id,
            release_title="The.Matrix.Reloaded.1080p.5.1.HDR",
            indexer="fake",
            size_bytes=0,
            seeders=9,
            leechers=0,
            download_url="magnet:?worse",
            fingerprint="fingerprint:worse",
        )
        session.add(worse_release)
        session.commit()
        worse_job = MediaJob(
            type=MediaType.MOVIE,
            title="The Matrix",
            release_title=worse_release.release_title,
            qbit_hash="hash:worse",
            category="skald-movie",
            status=JobStatus.ORGANIZED,
            source_subscription_id=better_job.source_subscription_id,
            source_subscription_release_id=worse_release.id,
        )
        session.add(worse_job)
        session.commit()

        first = record_organized_quality(session, better_job)[0]
        second = record_organized_quality(session, worse_job)[0]

        assert first.quality_score == [4, 4, 5]
        assert first.score_version == "v1"
        assert second.media_job_id == better_job.id
        baseline = session.exec(select(DownloadedQuality)).one()
        assert baseline.media_job_id == better_job.id
        assert baseline.size_bytes is None


def test_record_organized_quality_records_a_season_pack_and_skips_manual_jobs():
    engine = make_engine()
    with Session(engine) as session:
        _, _, job = sourced_job(
            session,
            media_type=MediaType.TV,
            title="Show",
            release_title="Show.S01.1080p.5.1.HDR",
            season=1,
            organization_mode=OrganizationMode.PACK,
        )
        manual = MediaJob(
            type=MediaType.MOVIE,
            title="Manual",
            release_title="Manual.2160p",
            qbit_hash="manual",
            category="skald-movie",
            status=JobStatus.ORGANIZED,
        )
        session.add(manual)
        session.commit()

        records = record_organized_quality(session, job)
        assert [record.target_key for record in records] == ["tv:tmdb:603:season:1:pack"]
        assert record_organized_quality(session, manual) == []


def test_record_organized_quality_allows_multiple_episode_targets_and_ignores_profile_edits():
    engine = make_engine()
    with Session(engine) as session:
        subscription, _, job = sourced_job(
            session,
            media_type=MediaType.TV,
            title="Show",
            release_title="Show.S01E01-E02.2160p.Atmos.DV",
            season=1,
            episode_set="[1,2]",
        )
        first = record_organized_quality(session, job)
        session.add(QualityProfile(
            media_type=MediaType.TV,
            allowed_resolutions=["480p"],
            minimum_seeders=99,
            excluded_tokens=["WEB"],
        ))
        session.commit()
        second = record_organized_quality(session, job)

        assert [record.target_key for record in first] == [
            "tv:tmdb:603:season:1:episode:1",
            "tv:tmdb:603:season:1:episode:2",
        ]
        assert [record.id for record in second] == [record.id for record in first]
        assert {tuple(record.quality_score) for record in session.exec(select(DownloadedQuality)).all()} == {
            (4, 4, 5)
        }
