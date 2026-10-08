import asyncio
import logging
import re
from pathlib import PurePath
from typing import Optional

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
import qbittorrentapi
from sqlalchemy import delete, func
from sqlmodel import select

from skald.auth import require_csrf
from skald.db import get_session
from skald.episodes import (
    format_episode_set_input,
    parse_episode_set_input,
    serialize_episode_set,
)
from skald.lifecycle import try_job_lock
from skald.flash import set_flash
from skald.models import (
    FileLifecycle,
    JobStatus,
    MediaJob,
    MediaType,
    OrganizationMode,
    OrganizedFile,
    _utcnow,
)
from skald.indexer.base import ReleaseResult
from skald.auth import require_csrf
from skald.services.grab import (
    MediaJobCreationError,
    TargetTorrentFileNotFoundError,
    TorrentMetadataUnavailableError,
    create_media_job,
    find_active_job_for_release,
)
from skald.worker import DeletionOutcome, reconcile_deleting_job, request_job_deletion

from skald.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

QUEUE_TAB_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.COMPLETED,
    JobStatus.ORGANIZING,
    JobStatus.DELETING,
)
ATTENTION_TAB_STATUSES = (JobStatus.NEEDS_ATTENTION, JobStatus.FAILED)
HISTORY_TAB_STATUSES = (JobStatus.ORGANIZED,)
# Terminal states a job can be removed from the list ("forget") without
# touching library files. Never while the worker may still act on the job.
FORGETTABLE_STATUSES = (JobStatus.ORGANIZED, JobStatus.FAILED, JobStatus.NEEDS_ATTENTION)
JOB_TABS = ("queue", "attention", "history")
HISTORY_PAGE_SIZE = 50
ERROR_EXCERPT_LENGTH = 140
_EPISODE_LABEL_RE = re.compile(r"S\d{1,2}E\d{1,3}(?:-?E\d{1,3})*", re.IGNORECASE)


def _job_ledger_rows(session, job_id: int) -> list[OrganizedFile]:
    return list(
        session.exec(
            select(OrganizedFile).where(OrganizedFile.job_id == job_id).order_by(OrganizedFile.path)
        ).all()
    )


def _is_delete_origin(job: MediaJob, rows: list[OrganizedFile]) -> bool:
    """A NEEDS_ATTENTION job whose ledger carries delete intent came from a blocked delete."""
    return job.status == JobStatus.NEEDS_ATTENTION and any(
        row.lifecycle == FileLifecycle.DELETE_REQUESTED for row in rows
    )


def _can_retry(job: MediaJob, rows: list[OrganizedFile]) -> bool:
    if job.status == JobStatus.FAILED:
        return True
    return job.status == JobStatus.NEEDS_ATTENTION and not _is_delete_origin(job, rows)


def _visible():
    """Jobs the operator has not removed from the lists."""
    return MediaJob.hidden_at.is_(None)


def _count_jobs(session, statuses) -> int:
    return session.exec(
        select(func.count(MediaJob.id)).where(MediaJob.status.in_(statuses)).where(_visible())
    ).one()


def error_excerpt(message: Optional[str]) -> str:
    text = " ".join((message or "").split())
    if len(text) <= ERROR_EXCERPT_LENGTH:
        return text
    return text[: ERROR_EXCERPT_LENGTH - 1].rstrip() + "…"


def active_jobs_payload(engine) -> dict:
    with get_session(engine) as session:
        queue_jobs = session.exec(
            select(MediaJob)
            .where(MediaJob.status.in_(QUEUE_TAB_STATUSES))
            .where(_visible())
            .order_by(MediaJob.created_at.desc())
        ).all()
        attention_count = _count_jobs(session, ATTENTION_TAB_STATUSES)
        history_count = _count_jobs(session, HISTORY_TAB_STATUSES)
    return {
        "jobs": [
            {
                "id": job.id,
                "type": job.type.value,
                "title": job.title,
                "season": job.season,
                "episode": job.episode,
                "episode_set": job.episode_set,
                "status": job.status.value,
                "progress": job.progress,
            }
            for job in queue_jobs
        ],
        "attention_count": attention_count,
        "history_count": history_count,
    }


async def wait_for_websocket_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


def validate_episode_set(
    media_type: str | MediaType,
    episode: Optional[int],
    episode_set: Optional[str],
    episode_set_supplied: bool,
) -> Optional[str]:
    """Validate form episode-set input and return its persisted representation."""
    if not episode_set_supplied:
        return None
    if media_type != MediaType.TV and media_type != MediaType.TV.value:
        raise HTTPException(status_code=422, detail="episode sets are only valid for TV")

    try:
        episodes = parse_episode_set_input(episode_set or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid episode set: {exc}") from exc
    if episode != episodes[0]:
        raise HTTPException(status_code=422, detail="episode must match the episode set start")
    return serialize_episode_set(episodes) if len(episodes) > 1 else None


def validate_job_submission(
    media_type: str | MediaType,
    title: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    episode_set: Optional[str],
    episode_set_supplied: bool,
) -> Optional[str]:
    """Validate shared grab/retry metadata before any side effects or mutation."""
    if not title or not title.strip():
        raise HTTPException(status_code=422, detail="title is required")
    if media_type == MediaType.TV or media_type == MediaType.TV.value:
        if season is None:
            raise HTTPException(status_code=422, detail="TV season is required")
        if episode is None:
            raise HTTPException(status_code=422, detail="TV episode is required")
    return validate_episode_set(media_type, episode, episode_set, episode_set_supplied)


def _episode_set_detail_context(job: Optional[MediaJob]) -> dict[str, str]:
    if job is None or not job.episode_set:
        return {"episode_label": "", "episode_set_input": ""}
    try:
        episodes = parse_episode_set_input(job.episode_set)
        episode_set_input = format_episode_set_input(episodes)
    except ValueError:
        return {"episode_label": "", "episode_set_input": ""}

    episode_label = ",".join(
        "-".join(f"E{int(episode):02d}" for episode in episode_range.split("-"))
        for episode_range in episode_set_input.split(",")
    )
    return {"episode_label": episode_label, "episode_set_input": episode_set_input}


def _tv_episode_label(job: MediaJob) -> str:
    """Return the compact season/episode label used by job-list TV rows."""
    episode_label = _episode_set_detail_context(job)["episode_label"]
    season = f"{job.season:02d}" if job.season else "?"
    episode = f"E{job.episode:02d}" if job.episode else "E?"
    return f"S{season}{episode_label or episode}"


def _search_back_url(return_q: str, return_type: str) -> str:
    if not return_q:
        return "/search"
    return f"/search?q={quote(return_q)}&type={quote(return_type or 'movie')}"


def _form_text(form, name: str) -> Optional[str]:
    value = form.get(name)
    return value if isinstance(value, str) else None


def _form_int(form, name: str) -> Optional[int]:
    """Parse an optional integer form field; blank means missing, 0 is kept."""
    value = _form_text(form, name)
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None


def _grab_failure_hint(exc: MediaJobCreationError) -> str:
    cause = exc.__cause__
    if isinstance(exc, TargetTorrentFileNotFoundError):
        return (
            "The torrent does not contain a file for the requested season/episode. "
            "Check the season and episode numbers or pick another release."
        )
    if isinstance(exc, TorrentMetadataUnavailableError):
        return (
            "qBittorrent did not fetch the torrent's file list in time. "
            "The torrent may have no peers yet; try again later."
        )
    if isinstance(cause, (qbittorrentapi.exceptions.APIConnectionError,
                          qbittorrentapi.exceptions.LoginFailed,
                          qbittorrentapi.exceptions.Forbidden403Error)):
        return "Check QBIT_HOST/QBIT_USER/QBIT_PASS."
    message = str(exc)
    if message.startswith(("Failed to download torrent", "Torrent download", "Downloaded file")):
        return "Could not download the .torrent from the indexer."
    return "qBittorrent rejected the request; see the server log for details."


@router.post("/grab", dependencies=[Depends(require_csrf)])
async def grab(request: Request):
    form = await request.form()
    release_title = _form_text(form, "release_title") or ""
    download_url = _form_text(form, "download_url") or ""
    media_type = _form_text(form, "media_type") or ""
    title = _form_text(form, "title")
    episode_set = _form_text(form, "episode_set")
    back_url = _search_back_url(
        _form_text(form, "return_q") or "", _form_text(form, "return_type") or media_type
    )

    def invalid(detail: str):
        submitted = ", ".join(
            f"{name}={form.get(name)!s}"
            for name in ("title", "year", "season", "episode", "episode_set")
            if form.get(name) not in (None, "")
        )
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Cannot add release",
                "detail": detail,
                "hint": (
                    f"Release: {release_title}. Submitted: {submitted or 'nothing'}."
                ),
                "back_url": back_url,
                "back_label": "Back to results",
            },
            status_code=422,
        )

    if not release_title or not download_url:
        return invalid("release title and download URL are required")
    if media_type not in (MediaType.MOVIE.value, MediaType.TV.value):
        return invalid("media type must be movie or tv")
    try:
        year = _form_int(form, "year")
        season = _form_int(form, "season")
        episode = _form_int(form, "episode")
    except ValueError as exc:
        return invalid(str(exc))

    try:
        persisted_episode_set = validate_job_submission(
            media_type,
            title,
            season,
            episode,
            episode_set,
            "episode_set" in form,
        )
        if media_type == "movie" and year is None:
            raise HTTPException(status_code=422, detail="movie year is required")
    except HTTPException as exc:
        return invalid(str(exc.detail))

    settings = request.app.state.settings
    qbit = request.app.state.qbit

    with get_session(request.app.state.engine) as session:
        existing = find_active_job_for_release(session, release_title, download_url)
        if existing is not None:
            return set_flash(
                RedirectResponse(url=f"/jobs/{existing.id}", status_code=303),
                "This release is already in the queue.",
                "info",
            )
        try:
            job = create_media_job(
                session,
                qbit,
                ReleaseResult(release_title, "manual", 0, 0, 0, download_url),
                media_type=MediaType(media_type),
                title=title,
                year=year,
                season=season,
                episode=episode,
                episode_set=persisted_episode_set,
                settings=settings,
            )
        except MediaJobCreationError as exc:
            logger.warning("Failed to add torrent: %s", exc, exc_info=True)
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "title": "Failed to add torrent",
                    "detail": str(exc),
                    "hint": _grab_failure_hint(exc),
                    "back_url": back_url,
                    "back_label": "Back to results",
                },
                status_code=502,
            )
        job_id = job.id
        job_title = job.title or release_title

    return set_flash(
        RedirectResponse(url=f"/jobs/{job_id}", status_code=303),
        f"Added “{job_title}” to the queue.",
    )


def _error_page(request: Request, title: str, detail: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "title": title,
            "detail": detail,
            "back_url": "/jobs",
            "back_label": "Back to jobs",
        },
        status_code=status_code,
    )


@router.post("/jobs/{job_id}/delete", dependencies=[Depends(require_csrf)])
async def delete_job(request: Request, job_id: int):
    with try_job_lock(job_id) as acquired:
        if not acquired:
            return HTMLResponse(
                "This job is already being updated; retry shortly.", status_code=409
            )
    qbit = request.app.state.qbit

    with get_session(request.app.state.engine) as session:
        existing = session.get(MediaJob, job_id)
        job_title = (existing.title if existing else None) or f"job #{job_id}"
        # Commit durable DELETING intent (and, for pack jobs, a fresh
        # delete token plus delete_requested ledger rows) before any
        # filesystem or qBittorrent side effect. Idempotent: a job already
        # DELETING (e.g. a retried delete click) is simply re-read so this
        # request can retry the same operation.
        job = request_job_deletion(session, job_id)
        if job is None:
            return RedirectResponse(url="/jobs", status_code=303)

        outcome = reconcile_deleting_job(session, job, qbit)

        if outcome == DeletionOutcome.LIBRARY_FAILURE:
            current = session.get(MediaJob, job_id)
            detail = current.error_message if current else "failed to remove library file"
            return _error_page(request, "Failed to delete library file", detail, 500)
        if outcome == DeletionOutcome.QBIT_FAILURE:
            current = session.get(MediaJob, job_id)
            detail = current.error_message if current else "failed to delete torrent"
            return _error_page(request, "Failed to delete torrent", detail, 502)
        if outcome == DeletionOutcome.NEEDS_ATTENTION:
            current = session.get(MediaJob, job_id)
            detail = current.error_message if current else "ownership conflict"
            return _error_page(request, "Delete blocked: ownership conflict", detail, 500)
        if outcome == DeletionOutcome.PENDING:
            return set_flash(
                RedirectResponse(url="/jobs", status_code=303),
                f"Deleting “{job_title}” is still in progress; it will be retried automatically.",
                "warning",
            )

    return set_flash(
        RedirectResponse(url="/jobs", status_code=303), f"Deleted “{job_title}”."
    )


@router.post("/jobs/{job_id}/forget", dependencies=[Depends(require_csrf)])
async def forget_job(request: Request, job_id: int):
    """Hide a finished job from the lists; library files are never touched.

    The row is kept (flagged) rather than deleted: DownloadedQuality baselines
    cascade-delete with their job and would silently break upgrade proposals,
    and subscription coverage reads the job rows. The torrent is removed from
    qBittorrent without its data so seeding files stay on disk.
    """
    qbit = request.app.state.qbit
    with get_session(request.app.state.engine) as session:
        with try_job_lock(job_id) as acquired:
            if not acquired:
                return HTMLResponse(
                    "This job is already being updated; retry shortly.", status_code=409
                )
            job = session.get(MediaJob, job_id)
            if job is None or job.hidden_at is not None:
                return RedirectResponse(url="/jobs", status_code=303)
            if job.status not in FORGETTABLE_STATUSES:
                return _error_page(
                    request,
                    "Cannot remove from list",
                    "Only organized, failed or needs-attention jobs can be removed from the list.",
                    409,
                )
            title = job.title
            try:
                qbit.delete_torrent(job.qbit_hash, delete_files=False)
            except LookupError:
                pass
            except Exception as exc:  # noqa: BLE001 - keep the job visible so it can be retried
                logger.warning("Failed to remove torrent for job %s: %s", job_id, exc, exc_info=True)
                return _error_page(request, "Failed to remove torrent", str(exc), 502)
            job.hidden_at = _utcnow()
            session.add(job)
            session.commit()
    return set_flash(
        RedirectResponse(url="/jobs", status_code=303),
        f"Removed “{title}” from the list. Files were kept.",
    )


@router.get("/jobs", response_class=HTMLResponse)
async def list_jobs(request: Request, tab: str = "", page: str = "1"):
    with get_session(request.app.state.engine) as session:
        attention_count = _count_jobs(session, ATTENTION_TAB_STATUSES)
        history_count = _count_jobs(session, HISTORY_TAB_STATUSES)
        queue_count = _count_jobs(session, QUEUE_TAB_STATUSES)
        if tab not in JOB_TABS:
            tab = "attention" if queue_count == 0 and attention_count > 0 else "queue"

        page_number = int(page) if page.isdigit() and int(page) > 0 else 1
        statuses = {
            "queue": QUEUE_TAB_STATUSES,
            "attention": ATTENTION_TAB_STATUSES,
            "history": HISTORY_TAB_STATUSES,
        }[tab]
        query = select(MediaJob).where(MediaJob.status.in_(statuses)).where(_visible())
        if tab == "history":
            query = (
                query.order_by(MediaJob.updated_at.desc(), MediaJob.id.desc())
                .offset((page_number - 1) * HISTORY_PAGE_SIZE)
                .limit(HISTORY_PAGE_SIZE)
            )
        else:
            query = query.order_by(MediaJob.created_at.desc())
        jobs = session.exec(query).all()

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "tab": tab,
            "queue_count": queue_count,
            "attention_count": attention_count,
            "history_count": history_count,
            "page": page_number,
            "newer_url": f"/jobs?tab=history&page={page_number - 1}" if page_number > 1 else None,
            "older_url": (
                f"/jobs?tab=history&page={page_number + 1}"
                if tab == "history" and page_number * HISTORY_PAGE_SIZE < history_count
                else None
            ),
            "error_excerpts": {job.id: error_excerpt(job.error_message) for job in jobs},
            "tv_episode_labels": {
                job.id: _tv_episode_label(job)
                for job in jobs
                if job.type == MediaType.TV and job.id is not None
            },
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    with get_session(request.app.state.engine) as session:
        job = session.get(MediaJob, job_id)
        if job is None or job.hidden_at is not None:
            return _error_page(
                request, "Job not found", f"No job with id {job_id} exists.", 404
            )
        rows = _job_ledger_rows(session, job_id)
        organized_files = [
            {
                "path": row.path,
                "name": PurePath(row.path).name,
                "episode_label": (
                    match.group(0).upper()
                    if (match := _EPISODE_LABEL_RE.search(PurePath(row.path).name))
                    else ""
                ),
                "lifecycle": row.lifecycle.value,
            }
            for row in rows
        ]
        # Paths a delete will actually remove (legacy rows are never auto-deleted).
        delete_paths = [
            row.path for row in rows if row.lifecycle != FileLifecycle.LEGACY_UNVERIFIED
        ]
        if job.library_path and job.library_path not in delete_paths:
            delete_paths.insert(0, job.library_path)
        context = {
            "job": job,
            "organized_files": organized_files,
            "delete_paths": delete_paths,
            "delete_origin": _is_delete_origin(job, rows),
            "can_retry": _can_retry(job, rows),
            "can_forget": job.status in FORGETTABLE_STATUSES,
            **_episode_set_detail_context(job),
        }
    return templates.TemplateResponse(request, "job_detail.html", context)


@router.websocket("/ws/jobs/active")
async def active_jobs_ws(websocket: WebSocket):
    await websocket.accept()
    last_payload = None
    disconnect_task = asyncio.create_task(wait_for_websocket_disconnect(websocket))
    try:
        while not disconnect_task.done():
            payload = await asyncio.to_thread(active_jobs_payload, websocket.app.state.engine)
            if payload != last_payload:
                await websocket.send_json(payload)
                last_payload = payload
            await asyncio.wait((disconnect_task,), timeout=2)
    except WebSocketDisconnect:
        pass
    finally:
        disconnect_task.cancel()
        try:
            await disconnect_task
        except asyncio.CancelledError:
            pass


@router.websocket("/ws/jobs/{job_id}")
async def job_status_ws(websocket: WebSocket, job_id: int):
    await websocket.accept()
    engine = websocket.app.state.engine
    last_payload = None
    terminal_statuses = (JobStatus.ORGANIZED, JobStatus.NEEDS_ATTENTION, JobStatus.FAILED)
    disconnect_task = asyncio.create_task(wait_for_websocket_disconnect(websocket))
    try:
        while not disconnect_task.done():
            with get_session(engine) as session:
                job = session.get(MediaJob, job_id)
            if job is None:
                await websocket.send_json({"status": "not_found"})
                break
            payload = {
                "status": job.status.value,
                "progress": job.progress,
                "error_message": job.error_message,
                "content_path": job.content_path,
                "terminal": job.status in terminal_statuses,
            }
            if payload != last_payload:
                await websocket.send_json(payload)
                last_payload = payload
                if job.status in terminal_statuses:
                    break
            await asyncio.wait((disconnect_task,), timeout=2)
    except WebSocketDisconnect:
        pass
    finally:
        disconnect_task.cancel()
        try:
            await disconnect_task
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            pass


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(require_csrf)])
async def retry_job(
    request: Request,
    job_id: int,
    title: Optional[str] = Form(None),
    year: Optional[int] = Form(None),
    season: Optional[int] = Form(None),
    episode: Optional[int] = Form(None),
    episode_set: Optional[str] = Form(None),
):
    with get_session(request.app.state.engine) as session:
        with try_job_lock(job_id) as acquired:
            if not acquired:
                return HTMLResponse(
                    "This job is already being updated; retry shortly.", status_code=409
                )
            job = session.get(MediaJob, job_id)
            if job is None:
                return RedirectResponse(url="/jobs", status_code=303)
            if job.status == JobStatus.DELETING:
                # Never destroy durable delete intent with a retry write.
                return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
            if not _can_retry(job, _job_ledger_rows(session, job_id)):
                return _error_page(
                    request,
                    "Retry not available",
                    "This job cannot be retried in its current state; "
                    "use Delete (or Retry delete) instead.",
                    409,
                )
            try:
                form = await request.form()
                persisted_episode_set = validate_job_submission(
                    job.type,
                    title,
                    season,
                    episode,
                    episode_set,
                    "episode_set" in form,
                )
                job.title = title
                job.year = year
                job.season = season
                job.episode = episode
                job.episode_set = persisted_episode_set
                if job.organization_mode == OrganizationMode.PACK:
                    session.execute(
                        delete(OrganizedFile)
                        .where(OrganizedFile.job_id == job.id)
                        .where(OrganizedFile.lifecycle != FileLifecycle.LEGACY_UNVERIFIED)
                    )
                    job.operation_token = None
                    job.organization_mode = OrganizationMode.SCALAR
                has_residual_pack_ledger = (
                    job.type == MediaType.TV
                    and session.exec(
                        select(OrganizedFile).where(OrganizedFile.job_id == job.id)
                    ).first() is not None
                )
                job.status = JobStatus.ORGANIZING if has_residual_pack_ledger else JobStatus.COMPLETED
                job.error_message = None
                session.add(job)
                session.commit()
                retry_title = job.title
            except Exception:
                session.rollback()
                raise
    return set_flash(
        RedirectResponse(url=f"/jobs/{job_id}", status_code=303),
        f"Retrying “{retry_title}”.",
    )
