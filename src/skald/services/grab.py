import time
from collections.abc import Callable, Sequence
from typing import Protocol, cast

from sqlmodel import Session

from skald.config import Settings
from skald.indexer.base import ReleaseResult
from skald.models import JobStatus, MediaJob, MediaType
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
    parsed = parse_release(file_name)
    return (
        parsed["media_type"] == MediaType.TV.value
        and parsed["season"] == season
        and bool(set(parsed["episode_set"]) & targets)
    )
