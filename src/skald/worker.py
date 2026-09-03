import asyncio
import logging
import uuid
from enum import Enum
from pathlib import Path

from sqlalchemy import exists, update
from sqlmodel import Session, select

from skald.episodes import deserialize_episode_set
from skald.lifecycle import FileIdentity, try_job_lock
from skald.models import (
    FileLifecycle,
    JobStatus,
    MediaJob,
    MediaType,
    OrganizationMode,
    OrganizedFile,
)
from skald.organizer import (
    TvPackError,
    build_tv_pack_targets,
    cleanup_owned_file,
    find_video_files,
    link_file,
    movie_target_path,
    prune_empty_parent,
    publish_staged_file,
    remove_organized_file,
    stage_file,
    staging_path_for,
    tv_target_path,
)
from skald.qbittorrent import QbittorrentClient

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.COMPLETED)

# Job statuses a pack organize attempt may legitimately start reserving
# from; used to fence reservation/preflight writes against a DELETING
# transition that may have landed concurrently.
ORGANIZABLE_STATUSES = (JobStatus.COMPLETED, JobStatus.QUEUED, JobStatus.DOWNLOADING)


class DeletionOutcome(str, Enum):
    """Result of attempting to reconcile a DELETING job's side effects."""

    DELETED = "deleted"
    LIBRARY_FAILURE = "library_failure"
    QBIT_FAILURE = "qbit_failure"
    NEEDS_ATTENTION = "needs_attention"
    PENDING = "pending"


async def poll_once(
    session: Session,
    qbit: QbittorrentClient,
    movies_root: str,
    tv_root: str,
) -> None:
    try:
        jobs = session.exec(select(MediaJob).where(MediaJob.status.in_(ACTIVE_STATUSES))).all()
        organizing_jobs = session.exec(
            select(MediaJob).where(MediaJob.status == JobStatus.ORGANIZING)
        ).all()
        deleting_jobs = session.exec(
            select(MediaJob).where(MediaJob.status == JobStatus.DELETING)
        ).all()
    except Exception:  # noqa: BLE001 - retry recovery discovery on the next poll
        logger.exception("failed to discover recovery jobs")
        session.rollback()
        return
    for job in organizing_jobs:
        try:
            reconcile_organizing_job(session, job)
        except Exception:  # noqa: BLE001 - preserve durable recovery state
            logger.exception("failed to reconcile organizing job %s", job.id)
            session.rollback()
    for job in deleting_jobs:
        try:
            reconcile_deleting_job(session, job, qbit)
        except Exception:  # noqa: BLE001 - preserve durable recovery state
            logger.exception("failed to reconcile deleting job %s", job.id)
            session.rollback()

    for job in jobs:
        try:
            await process_job(session, job, qbit, movies_root, tv_root)
        except Exception as exc:  # noqa: BLE001 - isolate per-job failures
            job_id = job.id
            session.rollback()
            durable_job = session.get(MediaJob, job_id, populate_existing=True)
            if durable_job is None or durable_job.status == JobStatus.DELETING:
                continue
            logger.exception("job %s failed", job.id)
            if durable_job.status in (JobStatus.ORGANIZING, JobStatus.DELETING):
                continue
            durable_job.status = JobStatus.FAILED
            durable_job.error_message = str(exc)
            session.add(durable_job)
            session.commit()


async def process_job(
    session: Session,
    job: MediaJob,
    qbit: QbittorrentClient,
    movies_root: str,
    tv_root: str,
) -> None:
    status = await asyncio.to_thread(qbit.get_status, job.qbit_hash)
    job = session.get(MediaJob, job.id, populate_existing=True)
    if job is None or job.status == JobStatus.DELETING:
        return
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
    content_path = Path(job.content_path)
    video_files = find_video_files(content_path)
    if not video_files:
        job.status = JobStatus.NEEDS_ATTENTION
        job.error_message = "No video files found in downloaded content"
        session.add(job)
        session.commit()
        return

    if job.type == MediaType.TV and len(video_files) > 1:
        organize_tv_pack(session, job, tv_root, video_files)
        return

    episode_set = ()
    if job.type == MediaType.TV:
        try:
            episode_set = deserialize_episode_set(job.episode_set)
        except ValueError as exc:
            job.status = JobStatus.NEEDS_ATTENTION
            job.error_message = f"Malformed episode_set metadata: {exc}"
            session.add(job)
            session.commit()
            return

    job.organization_mode = OrganizationMode.SCALAR
    job.status = JobStatus.ORGANIZING
    session.add(job)
    session.commit()

    source = video_files[0]
    ext = source.suffix

    if job.type == MediaType.MOVIE:
        target = movie_target_path(movies_root, job.title, job.year, ext)
    else:
        target = tv_target_path(
            tv_root,
            job.title,
            job.season,
            job.episode,
            ext,
            episode_set,
        )

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


def _conditional_organizing_write(session: Session, job_id: int, **values) -> bool:
    """Set a fresh (pre-reservation) pack organize outcome, fenced against a
    concurrent DELETING transition landing between the read and this write.
    """
    result = session.execute(
        update(MediaJob)
        .where(MediaJob.id == job_id)
        .where(MediaJob.status.in_(ORGANIZABLE_STATUSES))
        .values(**values)
    )
    if result.rowcount == 1:
        session.commit()
        return True
    session.rollback()
    return False


def organize_tv_pack(
    session: Session, job: MediaJob, tv_root: str, video_files: list[Path]
) -> None:
    """Organize a multi-video TV job as a token-fenced pack.

    Preflight happens without writes; reservation of every target commits in
    one transaction before any filesystem call; staging and publication are
    conditioned on the row's own operation token, so a stale attempt whose
    token has been replaced (e.g. by a delete request) can neither publish
    nor finalize.
    """
    with try_job_lock(job.id) as acquired:
        if not acquired:
            return

        try:
            mappings = build_tv_pack_targets(tv_root, job.title, video_files, job.season)
        except TvPackError as exc:
            _conditional_organizing_write(
                session, job.id, status=JobStatus.NEEDS_ATTENTION, error_message=str(exc)
            )
            return

        token = uuid.uuid4().hex
        reserved: list[tuple[OrganizedFile, Path, Path, Path]] = []
        try:
            for organized_file in session.exec(
                select(OrganizedFile).where(OrganizedFile.job_id == job.id)
            ).all():
                session.delete(organized_file)
            for source, target in mappings:
                staging_path = staging_path_for(target, token)
                row = OrganizedFile(
                    job_id=job.id,
                    path=str(target),
                    operation_token=token,
                    lifecycle=FileLifecycle.RESERVED,
                    staging_path=str(staging_path),
                )
                session.add(row)
                reserved.append((row, source, target, staging_path))
            # Conditioned on the job still being in a state that owns no
            # competing operation (e.g. not DELETING, whose commit may have
            # landed between our read and this write): a status committed by
            # another session must never be clobbered by a late reservation.
            result = session.execute(
                update(MediaJob)
                .where(MediaJob.id == job.id)
                .where(MediaJob.status.in_(ORGANIZABLE_STATUSES))
                .values(
                    organization_mode=OrganizationMode.PACK,
                    operation_token=token,
                    library_path=None,
                    error_message=None,
                    status=JobStatus.ORGANIZING,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return
            session.commit()
        except Exception as exc:  # noqa: BLE001 - never write without a durable ledger
            session.rollback()
            _conditional_organizing_write(
                session,
                job.id,
                status=JobStatus.NEEDS_ATTENTION,
                error_message=f"Failed to prepare TV pack organization: {exc}",
            )
            return

        job_id = job.id
        published_targets: list[Path] = []
        for row, source, target, staging_path in reserved:
            current_job = session.get(MediaJob, job_id, populate_existing=True)
            if current_job is None or current_job.operation_token != token:
                # Fenced out: another operation (e.g. a delete request) owns
                # this job now. Do no further work; recovery owns the state.
                return

            try:
                staged = stage_file(source, staging_path)
            except OSError as exc:
                _mark_pack_job_needs_attention(session, job_id, token, str(exc))
                return

            if not _transition_organized_file(
                session,
                job_id=job_id,
                row_id=row.id,
                token=token,
                expected_lifecycle=FileLifecycle.RESERVED,
                new_lifecycle=FileLifecycle.STAGED,
                staging_device=staged.identity.device,
                staging_inode=staged.identity.inode,
            ):
                return

            try:
                published = publish_staged_file(staged, target)
            except OSError as exc:
                _mark_pack_job_needs_attention(session, job_id, token, str(exc))
                return

            if not _transition_organized_file(
                session,
                job_id=job_id,
                row_id=row.id,
                token=token,
                expected_lifecycle=FileLifecycle.STAGED,
                new_lifecycle=FileLifecycle.PUBLISHED,
                published_device=published.identity.device,
                published_inode=published.identity.inode,
            ):
                return

            published_targets.append(target)

        _finalize_tv_pack_job(session, job_id, token, published_targets)


def _transition_organized_file(
    session: Session,
    *,
    job_id: int,
    row_id: int,
    token: str,
    expected_lifecycle: FileLifecycle,
    new_lifecycle: FileLifecycle,
    **fields,
) -> bool:
    """Conditionally advance a ledger row's lifecycle, fenced by its token.

    The row's own token/lifecycle are necessary but not sufficient: this
    also requires (in the same statement, not a prior in-memory read) that
    the owning job's current `operation_token` still matches, so a stale
    organizer whose token was replaced between the last check and this
    write can never advance a row.

    Returns False (a fencing loss, not an error) when the row's token or
    lifecycle, or the job's current token, no longer match what this
    attempt expects.
    """
    result = session.execute(
        update(OrganizedFile)
        .where(OrganizedFile.id == row_id)
        .where(OrganizedFile.operation_token == token)
        .where(OrganizedFile.lifecycle == expected_lifecycle)
        .where(
            exists().where(
                MediaJob.id == job_id, MediaJob.operation_token == token
            )
        )
        .values(lifecycle=new_lifecycle, **fields)
    )
    if result.rowcount == 1:
        session.commit()
        return True
    session.rollback()
    return False


def _finalize_tv_pack_job(
    session: Session, job_id: int, token: str, published_targets: list[Path]
) -> None:
    """Finalize ORGANIZING -> ORGANIZED only if every row for this token is published.

    A zero-row conditional update (stale job status or replaced token) is a
    fencing loss, not success; the caller must stop and let recovery own the
    job's state.
    """
    if not published_targets:
        return
    rows = session.exec(
        select(OrganizedFile).where(
            OrganizedFile.job_id == job_id, OrganizedFile.operation_token == token
        )
    ).all()
    if not rows or any(row.lifecycle != FileLifecycle.PUBLISHED for row in rows):
        return
    result = session.execute(
        update(MediaJob)
        .where(MediaJob.id == job_id)
        .where(MediaJob.status == JobStatus.ORGANIZING)
        .where(MediaJob.operation_token == token)
        .values(
            status=JobStatus.ORGANIZED,
            library_path=str(published_targets[0]),
            error_message=None,
        )
    )
    if result.rowcount == 1:
        session.commit()
    else:
        session.rollback()


def _mark_pack_job_needs_attention(
    session: Session, job_id: int, token: str, detail: str
) -> None:
    """Conditionally mark a pack job NEEDS_ATTENTION after a staging/publish failure.

    This must never silently overwrite a durable status/token committed by
    another operation (e.g. a delete request's `DELETING`). The update is
    fenced by both the expected `ORGANIZING` status and the current
    operation token; on a zero-row result the job's actual durable state is
    left untouched and recovery owns it.
    """
    try:
        result = session.execute(
            update(MediaJob)
            .where(MediaJob.id == job_id)
            .where(MediaJob.status == JobStatus.ORGANIZING)
            .where(MediaJob.operation_token == token)
            .values(status=JobStatus.NEEDS_ATTENTION, error_message=detail)
        )
    except Exception:  # noqa: BLE001 - retain the previously committed ledger for recovery
        session.rollback()
        return
    if result.rowcount == 1:
        session.commit()
    else:
        session.rollback()


def request_job_deletion(session: Session, job_id: int) -> MediaJob | None:
    """Commit durable DELETING intent before any filesystem or qBittorrent
    side effect.

    Acquires the shared per-job lock. For a pack job this replaces the
    operation token and marks every currently-owned (non-`legacy_unverified`)
    ledger row `delete_requested` with the new token in the same
    transaction: this fences out any organizer still mid-flight under the
    old token (its next conditional row/job write will find zero matching
    rows and stop). `legacy_unverified` rows are left untouched - they were
    never trustworthy enough to auto-delete and require operator
    reconciliation. A job already `DELETING` is idempotently re-read so the
    caller can retry reconciliation with the existing token/ledger.
    """
    with try_job_lock(job_id) as acquired:
        if not acquired:
            return session.get(MediaJob, job_id, populate_existing=True)
        job = session.get(MediaJob, job_id, populate_existing=True)
        if job is None:
            return None
        if job.status == JobStatus.DELETING:
            return job

        is_pack = job.organization_mode == OrganizationMode.PACK
        token = uuid.uuid4().hex if is_pack else job.operation_token
        job.status = JobStatus.DELETING
        job.error_message = None
        if is_pack:
            job.operation_token = token
        session.add(job)
        if is_pack:
            session.execute(
                update(OrganizedFile)
                .where(OrganizedFile.job_id == job_id)
                .where(OrganizedFile.lifecycle != FileLifecycle.LEGACY_UNVERIFIED)
                .values(lifecycle=FileLifecycle.DELETE_REQUESTED, operation_token=token)
            )
        session.commit()
        return job


def _public_identity_for_cleanup(row: OrganizedFile) -> FileIdentity | None:
    """Return the identity `row.path` (the public target) must carry for
    this attempt to safely delete it.

    Prefers the recorded published identity. When no published identity was
    ever recorded but a staging identity was, this is exactly the durable
    footprint of a crash or fencing loss between `publish_staged_file`
    succeeding (which hardlinks staging to `row.path` and then unlinks
    staging - see the hardlink-identity invariant asserted in
    `organizer.publish_staged_file`) and the `STAGED -> PUBLISHED` database
    write committing. In that window the public file already exists and,
    by that same hardlink invariant, carries exactly `staging_device`/
    `staging_inode`. Checking `row.path` against the staging identity here
    is what proves ownership in that window; without it, a completed
    publish whose DB transition never committed looks "clean" (staging_path
    already unlinked) and the ledger row is dropped while the real public
    file is silently orphaned forever.
    """
    if row.published_device is not None and row.published_inode is not None:
        return FileIdentity(row.published_device, row.published_inode)
    if row.staging_device is not None and row.staging_inode is not None:
        return FileIdentity(row.staging_device, row.staging_inode)
    return None


def reconcile_deleting_job(
    session: Session, job: MediaJob, qbit: QbittorrentClient
) -> DeletionOutcome:
    """Idempotently drive a durable DELETING job to completion.

    Runs under the shared per-job lock and re-reads durable state first, so
    it never acts on a stale in-memory job. For pack jobs, only rows whose
    `operation_token` matches the job's current (delete) token are ever
    touched, and only when their recorded identity matches what is on disk;
    a missing path is already clean, a present-but-mismatched or
    `legacy_unverified` row is left untouched and reported as an ownership
    conflict. qBittorrent deletion is attempted only after every owned
    filesystem artifact is confirmed absent; an already-absent torrent is
    success. Ledger rows and the job are removed only after both layers
    succeed.
    """
    with try_job_lock(job.id) as acquired:
        if not acquired:
            return DeletionOutcome.PENDING
        current = session.get(MediaJob, job.id, populate_existing=True)
        if current is None:
            return DeletionOutcome.DELETED
        if current.status != JobStatus.DELETING:
            return DeletionOutcome.PENDING
        job = current
        token = job.operation_token

        rows: list[OrganizedFile] = []
        if job.organization_mode == OrganizationMode.PACK:
            rows = session.exec(
                select(OrganizedFile).where(OrganizedFile.job_id == job.id)
            ).all()
            blocked_paths: list[str] = []
            cleanup_errors: list[str] = []
            for row in rows:
                if row.lifecycle == FileLifecycle.LEGACY_UNVERIFIED:
                    blocked_paths.append(row.path)
                    continue
                if row.operation_token != token:
                    blocked_paths.append(row.path)
                    continue

                public_identity = _public_identity_for_cleanup(row)
                if public_identity is None:
                    # Never reached a confirmed identity (still `reserved`):
                    # nothing was ever published or staged; dropping the
                    # reservation is safe without touching the filesystem.
                    continue
                outcome = cleanup_owned_file(Path(row.path), public_identity)
                if outcome.foreign:
                    blocked_paths.append(row.path)
                    continue
                if outcome.error:
                    cleanup_errors.append(outcome.error)
                    continue

                # A row may also still have an un-cleaned private staging
                # copy: a completed publish already unlinked it (this is a
                # no-op), but an interrupted one, or the crash/fence-loss
                # window above, can leave it behind.
                if (
                    row.staging_path
                    and row.staging_device is not None
                    and row.staging_inode is not None
                ):
                    staging_outcome = cleanup_owned_file(
                        Path(row.staging_path),
                        FileIdentity(row.staging_device, row.staging_inode),
                    )
                    if staging_outcome.foreign:
                        blocked_paths.append(row.path)
                        continue
                    if staging_outcome.error:
                        cleanup_errors.append(staging_outcome.error)

            if blocked_paths:
                # A genuine ownership conflict (foreign identity, stale
                # token, or an untrusted legacy row) requires operator
                # reconciliation; it is never automatically retried.
                detail_parts = [
                    "ownership conflict for: " + "; ".join(sorted(set(blocked_paths)))
                ]
                if cleanup_errors:
                    detail_parts.append("cleanup failed: " + "; ".join(cleanup_errors))
                _mark_deleting_job_needs_attention(
                    session, job.id, token, "; ".join(detail_parts)
                )
                return DeletionOutcome.NEEDS_ATTENTION
            if cleanup_errors:
                # A transient filesystem error on an otherwise-owned file
                # (e.g. a permission problem) keeps durable DELETING intent
                # and the full ledger for automatic retry on the next poll.
                persist_deletion_failure(
                    session,
                    job,
                    "Delete pending: cleanup failed: " + "; ".join(cleanup_errors),
                )
                return DeletionOutcome.LIBRARY_FAILURE

            # Every owned file for this job has now been unlinked (above).
            # Prune now-empty parent directories the same way scalar
            # deletion does via remove_organized_file, so a fully deleted
            # pack doesn't leave an empty Show/Season 01/ tree behind. Only
            # reached once every row is confirmed clean, so this never
            # prunes a directory that still holds a sibling episode's file
            # pending cleanup earlier in this same job.
            for row in rows:
                prune_empty_parent(Path(row.path))
                if row.staging_path:
                    prune_empty_parent(Path(row.staging_path))
        else:
            # Scalar deletion never uses pack identity fields; it only
            # removes the metadata-derived `library_path`.
            try:
                if job.library_path:
                    remove_organized_file(Path(job.library_path))
            except OSError as exc:
                persist_deletion_failure(
                    session, job, f"Delete pending: failed to remove library file: {exc}"
                )
                return DeletionOutcome.LIBRARY_FAILURE

        try:
            qbit.delete_torrent(job.qbit_hash, delete_files=True)
        except LookupError:
            pass
        except Exception as exc:  # noqa: BLE001 - retain deletion intent for retry
            persist_deletion_failure(
                session, job, f"Delete pending: failed to delete torrent: {exc}"
            )
            return DeletionOutcome.QBIT_FAILURE

        for row in rows:
            session.delete(row)
        session.delete(job)
        try:
            session.commit()
        except Exception:  # noqa: BLE001 - preserve durable DELETING state for retry
            session.rollback()
            return DeletionOutcome.PENDING
        return DeletionOutcome.DELETED


def _mark_deleting_job_needs_attention(
    session: Session, job_id: int, token: str | None, detail: str
) -> None:
    query = (
        update(MediaJob)
        .where(MediaJob.id == job_id)
        .where(MediaJob.status == JobStatus.DELETING)
    )
    if token is not None:
        query = query.where(MediaJob.operation_token == token)
    result = session.execute(
        query.values(status=JobStatus.NEEDS_ATTENTION, error_message=detail)
    )
    if result.rowcount == 1:
        session.commit()
    else:
        session.rollback()


def persist_deletion_failure(session: Session, job: MediaJob, detail: str) -> None:
    result = session.execute(
        update(MediaJob)
        .where(MediaJob.id == job.id)
        .where(MediaJob.status == JobStatus.DELETING)
        .values(error_message=detail)
    )
    if result.rowcount == 1:
        session.commit()
    else:
        session.rollback()


def reconcile_organizing_job(session: Session, job: MediaJob) -> None:
    """Idempotently recover an interrupted ORGANIZING job.

    Runs under the shared per-job lock and re-reads durable state first. A
    pack job is recoverable only by its current `operation_token`: rows
    carrying a different (stale) token, or the `legacy_unverified` lifecycle,
    are never touched automatically and force `NEEDS_ATTENTION` for operator
    reconciliation. An owned row's abandoned staging/public artifact is only
    removed after its recorded identity matches what is on disk; a `reserved`
    row never had a confirmed identity, so its reservation is simply
    released without any filesystem action. When every row is cleanly
    accounted for, the ledger is cleared and the job returns to `COMPLETED`
    so the next poll retries organization from scratch.
    """
    with try_job_lock(job.id) as acquired:
        if not acquired:
            return
        current = session.get(MediaJob, job.id, populate_existing=True)
        if current is None or current.status != JobStatus.ORGANIZING:
            return
        job = current

        if job.organization_mode != OrganizationMode.PACK:
            # No ledger to identity-check. link_file's own existence guard
            # makes a blind retry safe: if it already succeeded, retry
            # surfaces a clear "already exists" conflict instead of
            # silently duplicating or losing data.
            result = session.execute(
                update(MediaJob)
                .where(MediaJob.id == job.id)
                .where(MediaJob.status == JobStatus.ORGANIZING)
                .values(status=JobStatus.COMPLETED, error_message=None, library_path=None)
            )
            if result.rowcount == 1:
                session.commit()
            else:
                session.rollback()
            return

        token = job.operation_token
        rows = session.exec(
            select(OrganizedFile).where(OrganizedFile.job_id == job.id)
        ).all()
        blocked_paths: list[str] = []
        cleanup_errors: list[str] = []
        for row in rows:
            if row.lifecycle == FileLifecycle.LEGACY_UNVERIFIED:
                blocked_paths.append(row.path)
                continue
            if row.operation_token != token:
                blocked_paths.append(row.path)
                continue
            if row.lifecycle == FileLifecycle.RESERVED:
                # No confirmed identity was ever captured; nothing on disk
                # can be safely attributed to this row. Just release the
                # reservation.
                continue
            if row.lifecycle == FileLifecycle.PUBLISHED:
                if row.published_device is None or row.published_inode is None:
                    blocked_paths.append(row.path)
                    continue
                outcome = cleanup_owned_file(
                    Path(row.path),
                    FileIdentity(row.published_device, row.published_inode),
                )
                if outcome.foreign:
                    blocked_paths.append(row.path)
                    continue
                if outcome.error:
                    cleanup_errors.append(outcome.error)
                    continue
                if (
                    row.staging_path
                    and row.staging_device is not None
                    and row.staging_inode is not None
                ):
                    staging_outcome = cleanup_owned_file(
                        Path(row.staging_path),
                        FileIdentity(row.staging_device, row.staging_inode),
                    )
                    if staging_outcome.foreign:
                        blocked_paths.append(row.path)
                        continue
                    if staging_outcome.error:
                        cleanup_errors.append(staging_outcome.error)
                continue
            if row.lifecycle == FileLifecycle.STAGED:
                if row.staging_device is None or row.staging_inode is None:
                    blocked_paths.append(row.path)
                    continue
                staging_identity = FileIdentity(row.staging_device, row.staging_inode)
                # Fence-loss window: publish_staged_file may have already
                # hardlinked row.path (and unlinked staging) before the
                # STAGED -> PUBLISHED database write committed. By the
                # hardlink invariant asserted in publish_staged_file, a
                # public file at row.path, if present, carries exactly this
                # staging identity - checking it here is what proves
                # ownership in that window (see _public_identity_for_cleanup
                # for the full explanation).
                outcome = cleanup_owned_file(Path(row.path), staging_identity)
                if outcome.foreign:
                    blocked_paths.append(row.path)
                    continue
                if outcome.error:
                    cleanup_errors.append(outcome.error)
                    continue
                if row.staging_path:
                    staging_outcome = cleanup_owned_file(Path(row.staging_path), staging_identity)
                    if staging_outcome.foreign:
                        blocked_paths.append(row.path)
                        continue
                    if staging_outcome.error:
                        cleanup_errors.append(staging_outcome.error)
                continue
            # Unknown/unexpected lifecycle value: stop automatic mutation
            # rather than guess.
            blocked_paths.append(row.path)

        if blocked_paths:
            # A genuine ownership conflict requires operator reconciliation;
            # it is never automatically retried.
            detail_parts = [
                "ownership conflict for: " + "; ".join(sorted(set(blocked_paths)))
            ]
            if cleanup_errors:
                detail_parts.append("cleanup failed: " + "; ".join(cleanup_errors))
            result = session.execute(
                update(MediaJob)
                .where(MediaJob.id == job.id)
                .where(MediaJob.status == JobStatus.ORGANIZING)
                .where(MediaJob.operation_token == token)
                .values(
                    status=JobStatus.NEEDS_ATTENTION,
                    error_message="; ".join(detail_parts),
                    library_path=None,
                )
            )
            if result.rowcount == 1:
                session.commit()
            else:
                session.rollback()
            return
        if cleanup_errors:
            # A transient filesystem error on an otherwise-owned artifact:
            # leave the job ORGANIZING so the next poll retries cleanup,
            # matching the durable-retry behavior deletion uses. Record the
            # detail for visibility (mirroring "Delete pending: ..." on the
            # deleting side) instead of leaving error_message untouched.
            result = session.execute(
                update(MediaJob)
                .where(MediaJob.id == job.id)
                .where(MediaJob.status == JobStatus.ORGANIZING)
                .where(MediaJob.operation_token == token)
                .values(
                    error_message="Recovery pending: cleanup failed: "
                    + "; ".join(cleanup_errors)
                )
            )
            if result.rowcount == 1:
                session.commit()
            else:
                session.rollback()
            return

        # Every owned artifact for this job has now been unlinked (above).
        # Prune now-empty parent directories the same way scalar deletion
        # does via remove_organized_file, so an interrupted-then-cleaned
        # pack doesn't leave an empty Show/Season 01/ tree behind.
        for row in rows:
            prune_empty_parent(Path(row.path))
            if row.staging_path:
                prune_empty_parent(Path(row.staging_path))

        for row in rows:
            session.delete(row)
        result = session.execute(
            update(MediaJob)
            .where(MediaJob.id == job.id)
            .where(MediaJob.status == JobStatus.ORGANIZING)
            .where(MediaJob.operation_token == token)
            .values(status=JobStatus.COMPLETED, error_message=None, library_path=None)
        )
        if result.rowcount == 1:
            session.commit()
        else:
            session.rollback()


async def worker_loop(
    session_factory,
    qbit: QbittorrentClient,
    movies_root: str,
    tv_root: str,
    poll_interval_seconds: int,
) -> None:
    while True:
        session = None
        try:
            with session_factory() as session:
                await poll_once(session, qbit, movies_root, tv_root)
        except Exception:  # noqa: BLE001 - keep the worker alive across transient database failures
            logger.exception("worker poll failed")
            if session is not None:
                session.rollback()
        await asyncio.sleep(poll_interval_seconds)
