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

    # Target identity still comes from the job's release title, but observed
    # quality deliberately comes from the sourced release below.  The latter
    # is the durable snapshot selected at subscription-scan time.
    try:
        parsed = parse_release(job.release_title)
    except Exception:  # noqa: BLE001 - title parsing is best-effort derivation
        return []
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
    """Stage only strictly better current baselines for an ORGANIZED job.

    This function intentionally does not commit.  Its caller must commit the
    baseline together with the ORGANIZING -> ORGANIZED transition, so a crash
    cannot leave an organized job without its required source-backed baseline.
    """
    target_keys = target_keys_for_job(session, job)
    if not target_keys:
        return []

    release = session.get(SubscriptionRelease, job.source_subscription_release_id)
    if release is None:
        return []

    profile_service = QualityProfileService()
    # SubscriptionRelease holds normalized observation captured at discovery;
    # do not re-derive baseline quality from a potentially changed/unparseable
    # job title.  observed_from_parsed also rejects corrupted noncanonical
    # durable values and invalid sizes.
    observed = profile_service.observed_from_parsed(
        {
            "resolution": release.resolution,
            "audio": release.audio,
            "hdr": release.hdr,
        },
        release.size_bytes,
    )
    if (
        observed.resolution == "unknown"
        and observed.audio == "unknown"
        and observed.hdr == "unknown"
        and observed.size_bytes is None
    ):
        # A (0, 0, 0) score with no durable observed attribute is
        # indeterminate, not a meaningful upgrade baseline.
        return []
    score = profile_service.fixed_score(observed)

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
