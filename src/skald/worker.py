import asyncio
import logging
from pathlib import Path

from sqlmodel import Session, select

from skald.models import JobStatus, MediaJob, MediaType
from skald.organizer import find_video_files, link_file, movie_target_path, tv_target_path
from skald.qbittorrent import QbittorrentClient

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.COMPLETED)


async def poll_once(
    session: Session,
    qbit: QbittorrentClient,
    movies_root: str,
    tv_root: str,
) -> None:
    jobs = session.exec(select(MediaJob).where(MediaJob.status.in_(ACTIVE_STATUSES))).all()
    for job in jobs:
        try:
            await process_job(session, job, qbit, movies_root, tv_root)
        except Exception as exc:  # noqa: BLE001 - isolate per-job failures
            logger.exception("job %s failed", job.id)
            job.status = JobStatus.FAILED
            job.error_message = str(exc)
            session.add(job)
            session.commit()


async def process_job(
    session: Session,
    job: MediaJob,
    qbit: QbittorrentClient,
    movies_root: str,
    tv_root: str,
) -> None:
    status = await asyncio.to_thread(qbit.get_status, job.qbit_hash)
    job.progress = status.progress
    job.content_path = status.content_path

    if not status.is_complete:
        job.status = JobStatus.DOWNLOADING
        session.add(job)
        session.commit()
        return

    job.status = JobStatus.COMPLETED
    session.add(job)
    session.commit()

    organize_job(session, job, movies_root, tv_root)


def organize_job(session: Session, job: MediaJob, movies_root: str, tv_root: str) -> None:
    job.status = JobStatus.ORGANIZING
    session.add(job)
    session.commit()

    content_path = Path(job.content_path)
    video_files = find_video_files(content_path)
    if not video_files:
        job.status = JobStatus.NEEDS_ATTENTION
        job.error_message = "No video files found in downloaded content"
        session.add(job)
        session.commit()
        return

    source = video_files[0]
    ext = source.suffix

    if job.type == MediaType.MOVIE:
        target = movie_target_path(movies_root, job.title, job.year, ext)
    else:
        target = tv_target_path(tv_root, job.title, job.season, job.episode, ext)

    try:
        link_file(source, target)
    except FileExistsError as exc:
        job.status = JobStatus.NEEDS_ATTENTION
        job.error_message = str(exc)
        session.add(job)
        session.commit()
        return

    job.library_path = str(target)
    job.status = JobStatus.ORGANIZED
    session.add(job)
    session.commit()


async def worker_loop(
    session_factory,
    qbit: QbittorrentClient,
    movies_root: str,
    tv_root: str,
    poll_interval_seconds: int,
) -> None:
    while True:
        with session_factory() as session:
            await poll_once(session, qbit, movies_root, tv_root)
        await asyncio.sleep(poll_interval_seconds)
