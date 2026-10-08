import base64
import re
import time
from collections.abc import Callable, Sequence
from typing import Protocol, cast
from urllib.parse import parse_qs, urlparse

from sqlmodel import Session, col, select

from skald.config import Settings
from skald.indexer.base import ReleaseResult
from skald.models import JobStatus, MediaJob, MediaType
from skald.episode_files import AmbiguousEpisodeMarkers, resolve_file_episodes
from skald.parser import parse_release


class TorrentAdder(Protocol):
    def add_torrent(self, download_url: str, category: str) -> str: ...


class SelectiveTorrentAdder(TorrentAdder, Protocol):
    def add_torrent_paused(self, download_url: str, category: str) -> str: ...

    def get_torrent_files(self, torrent_hash: str) -> Sequence["TorrentFileInfo"]: ...

    def set_file_priority(
        self, torrent_hash: str, file_indexes: list[int], priority: int
    ) -> None: ...

    def resume_torrent(self, torrent_hash: str) -> None: ...


class TorrentFileInfo(Protocol):
    index: int
    name: str


class MediaJobCreationError(Exception):
    """qBittorrent prevented a media job from being created."""


class TargetTorrentFileNotFoundError(MediaJobCreationError):
    """A paused torrent did not contain a file for the requested episode."""


class TorrentMetadataUnavailableError(MediaJobCreationError):
    """A paused torrent did not expose file metadata before polling expired."""


_INACTIVE_STATUSES = (
    JobStatus.ORGANIZED,
    JobStatus.NEEDS_ATTENTION,
    JobStatus.FAILED,
    JobStatus.DELETING,
)


def extract_info_hash(download_url: str) -> str | None:
    """Return the lowercase hex BitTorrent info hash of a magnet link, if any."""
    if not download_url.lower().startswith("magnet:"):
        return None
    for value in parse_qs(urlparse(download_url).query).get("xt", []):
        if not value.lower().startswith("urn:btih:"):
            continue
        digest = value[len("urn:btih:"):]
        if re.fullmatch(r"[0-9a-fA-F]{40}", digest):
            return digest.lower()
        if re.fullmatch(r"[A-Za-z2-7]{32}", digest):
            return base64.b32decode(digest.upper()).hex()
    return None


def find_active_job_for_release(
    session: Session, release_title: str, download_url: str
) -> MediaJob | None:
    """Find a non-terminal job already created for this release.

    Matches by info hash when the link carries one, otherwise by release title
    (jobs do not persist the download URL).
    """
    jobs = session.exec(
        select(MediaJob)
        .where(col(MediaJob.status).not_in(_INACTIVE_STATUSES))
        .order_by(col(MediaJob.id).desc())
    ).all()
    info_hash = extract_info_hash(download_url)
    for job in jobs:
        if info_hash is not None:
            if job.qbit_hash.lower() == info_hash:
                return job
        elif job.release_title == release_title:
            return job
    return None


def create_media_job(
    session: Session,
    qbit: TorrentAdder,
    release: ReleaseResult,
    *,
    media_type: MediaType,
    title: str,
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
    episode_set: str | None = None,
    target_episode_numbers: Sequence[int] | None = None,
    source_subscription_id: int | None = None,
    source_subscription_release_id: int | None = None,
    settings: Settings,
    metadata_poll_attempts: int = 10,
    metadata_poll_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> MediaJob:
    """Add a release to qBittorrent and persist its queued media job."""
    category = (
        settings.category_movie if media_type == MediaType.MOVIE else settings.category_tv
    )
    try:
        if target_episode_numbers is None:
            torrent_hash = qbit.add_torrent(release.download_url, category)
        else:
            torrent_hash = _add_targeted_tv_torrent(
                cast(SelectiveTorrentAdder, qbit),
                release.download_url,
                category,
                season=season,
                target_episode_numbers=target_episode_numbers,
                metadata_poll_attempts=metadata_poll_attempts,
                metadata_poll_interval_seconds=metadata_poll_interval_seconds,
                sleep=sleep,
            )
    except MediaJobCreationError:
        raise
    except Exception as exc:  # noqa: BLE001 - callers map qBittorrent failures for their context
        raise MediaJobCreationError(str(exc)) from exc

    job = MediaJob(
        type=media_type,
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_set=episode_set,
        release_title=release.title,
        qbit_hash=torrent_hash,
        category=category,
        status=JobStatus.QUEUED,
        source_subscription_id=source_subscription_id,
        source_subscription_release_id=source_subscription_release_id,
    )
    session.add(job)
    session.commit()
    return job


def _add_targeted_tv_torrent(
    qbit: SelectiveTorrentAdder,
    download_url: str,
    category: str,
    *,
    season: int | None,
    target_episode_numbers: Sequence[int],
    metadata_poll_attempts: int,
    metadata_poll_interval_seconds: float,
    sleep: Callable[[float], None],
) -> str:
    if season is None:
        raise ValueError("targeted TV downloads require a season")
    targets = set(target_episode_numbers)
    if not targets:
        raise ValueError("targeted TV downloads require at least one episode")
    if metadata_poll_attempts < 1:
        raise ValueError("metadata_poll_attempts must be at least 1")

    torrent_hash = qbit.add_torrent_paused(download_url, category)
    files = ()
    for attempt in range(metadata_poll_attempts):
        files = qbit.get_torrent_files(torrent_hash)
        if files:
            break
        if attempt < metadata_poll_attempts - 1:
            sleep(metadata_poll_interval_seconds)
    if not files:
        raise TorrentMetadataUnavailableError(
            "qBittorrent did not provide torrent file metadata"
        )

    all_indexes = [file.index for file in files]
    target_indexes = [
        file.index
        for file in files
        if _file_matches_target_episode(file.name, season, targets)
    ]
    if not target_indexes:
        raise TargetTorrentFileNotFoundError(
            "torrent contains no file for the requested episode"
        )

    # A torrent is paused until both priority calls have succeeded. This means
    # any API failure leaves it safely paused rather than downloading a pack.
    qbit.set_file_priority(torrent_hash, all_indexes, priority=0)
    qbit.set_file_priority(torrent_hash, target_indexes, priority=1)
    qbit.resume_torrent(torrent_hash)
    return torrent_hash


def _file_matches_target_episode(file_name: str, season: int, targets: set[int]) -> bool:
    # guessit first: it understands multi-episode and other release styles.
    parsed = parse_release(file_name)
    if parsed["media_type"] == MediaType.TV.value and parsed["season"] is not None:
        return parsed["season"] == season and bool(set(parsed["episode_set"]) & targets)
    try:
        file_season, episodes = resolve_file_episodes(file_name, season)
    except AmbiguousEpisodeMarkers:
        return False
    return file_season == season and bool(set(episodes) & targets)
