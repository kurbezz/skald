"""Durably record profile-independent quality for organized subscription jobs."""

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from skald.episodes import deserialize_episode_set
from skald.models import (
    DownloadedQuality,
    JobStatus,
    MediaJob,
    MediaSubscription,
    MediaType,
    OrganizationMode,
    SubscriptionRelease,
)
from skald.parser import parse_release
from skald.quality import QUALITY_SCORE_VERSION, QualityProfileService


def target_keys_for_job(session: Session, job: MediaJob) -> list[str]:
    """Return canonical source-backed target keys in lexicographic order."""
    if (
        job.status is not JobStatus.ORGANIZED
        or job.source_subscription_id is None
        or job.source_subscription_release_id is None
    ):
        return []
    subscription = session.get(MediaSubscription, job.source_subscription_id)
    release = session.get(SubscriptionRelease, job.source_subscription_release_id)
    if (
        subscription is None
        or release is None
        or subscription.type is not job.type
        or release.subscription_id != subscription.id
    ):
        return []
    if job.type is MediaType.MOVIE:
        return [f"movie:tmdb:{subscription.tmdb_id}"]
    if job.type is not MediaType.TV:
        return []

    parsed = parse_release(job.release_title)
    season = parsed.get("season")
    if not isinstance(season, int) or isinstance(season, bool):
        return []
    parsed_episode_set = parsed.get("episode_set")
    if not isinstance(parsed_episode_set, (tuple, list, set)):
        return []
    parsed_episodes = {
        episode
        for episode in parsed_episode_set
        if isinstance(episode, int) and not isinstance(episode, bool) and episode > 0
    }
    try:
        durable_episodes = set(deserialize_episode_set(job.episode_set))
    except ValueError:
        return []
    if parsed_episodes:
        if not durable_episodes or not durable_episodes.issubset(parsed_episodes):
            return []
        return sorted(
            f"tv:tmdb:{subscription.tmdb_id}:season:{season}:episode:{episode}"
            for episode in durable_episodes
        )
    if job.organization_mode is OrganizationMode.PACK:
        return [f"tv:tmdb:{subscription.tmdb_id}:season:{season}:pack"]
    return []


def record_organized_quality(session: Session, job: MediaJob) -> list[DownloadedQuality]:
    """Upsert only strictly better current baselines for an ORGANIZED job."""
    target_keys = target_keys_for_job(session, job)
    if not target_keys:
        return []

    profile_service = QualityProfileService()
    observed = profile_service.observed_from_parsed(parse_release(job.release_title), None)
    score = profile_service.fixed_score(observed)
    if len(score) != 3:
        return []

    recorded: list[DownloadedQuality] = []
    for target_key in target_keys:
        existing = session.exec(
            select(DownloadedQuality).where(
                DownloadedQuality.media_type == job.type,
                DownloadedQuality.target_key == target_key,
            )
        ).first()
        if existing is not None:
            if score > tuple(existing.quality_score):
                _replace_baseline(existing, job, observed, score)
            recorded.append(existing)
            continue

        baseline = DownloadedQuality(
            media_type=job.type,
            target_key=target_key,
            subscription_id=job.source_subscription_id,
            media_job_id=job.id,
            resolution=observed.resolution,
            audio=observed.audio,
            hdr=observed.hdr,
            size_bytes=observed.size_bytes,
            score_version=QUALITY_SCORE_VERSION,
            quality_score=list(score),
        )
        try:
            with session.begin_nested():
                session.add(baseline)
                session.flush()
        except IntegrityError:
            existing = session.exec(
                select(DownloadedQuality).where(
                    DownloadedQuality.media_type == job.type,
                    DownloadedQuality.target_key == target_key,
                )
            ).first()
            if existing is None:
                raise
            if score > tuple(existing.quality_score):
                _replace_baseline(existing, job, observed, score)
            recorded.append(existing)
        else:
            recorded.append(baseline)
    session.commit()
    return recorded


def _replace_baseline(
    baseline: DownloadedQuality,
    job: MediaJob,
    observed,
    score: tuple[int, int, int],
) -> None:
    baseline.media_job_id = job.id
    baseline.subscription_id = job.source_subscription_id
    baseline.resolution = observed.resolution
    baseline.audio = observed.audio
    baseline.hdr = observed.hdr
    baseline.size_bytes = observed.size_bytes
    baseline.score_version = QUALITY_SCORE_VERSION
    baseline.quality_score = list(score)
    baseline.updated_at = datetime.now(timezone.utc)
