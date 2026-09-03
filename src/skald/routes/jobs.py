import asyncio
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete
from sqlmodel import select

from skald.db import get_session
from skald.episodes import (
    format_episode_set_input,
    parse_episode_set_input,
    serialize_episode_set,
)
from skald.lifecycle import try_job_lock
from skald.models import FileLifecycle, JobStatus, MediaJob, MediaType, OrganizationMode, OrganizedFile
from skald.worker import DeletionOutcome, reconcile_deleting_job, request_job_deletion

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")

ACTIVE_TAB_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.COMPLETED,
    JobStatus.ORGANIZING,
    JobStatus.DELETING,
)
COMPLETED_TAB_STATUSES = (
    JobStatus.ORGANIZED,
    JobStatus.NEEDS_ATTENTION,
    JobStatus.FAILED,
)


def active_jobs_payload(engine) -> dict:
    with get_session(engine) as session:
        all_jobs = session.exec(select(MediaJob).order_by(MediaJob.created_at.desc())).all()

    active_jobs = [job for job in all_jobs if job.status in ACTIVE_TAB_STATUSES]
    completed_count = sum(job.status in COMPLETED_TAB_STATUSES for job in all_jobs)
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
            for job in active_jobs
        ],
        "completed_count": completed_count,
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


@router.post("/grab")
async def grab(
    request: Request,
    release_title: str = Form(...),
    download_url: str = Form(...),
    media_type: str = Form(...),
    title: Optional[str] = Form(None),
    year: Optional[int] = Form(None),
    season: Optional[int] = Form(None),
    episode: Optional[int] = Form(None),
    episode_set: Optional[str] = Form(None),
):
    form = await request.form()
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

    settings = request.app.state.settings
    qbit = request.app.state.qbit
    category = settings.category_movie if media_type == "movie" else settings.category_tv

    try:
        torrent_hash = qbit.add_torrent(download_url, category)
    except Exception as exc:  # noqa: BLE001 - surface any qBittorrent failure to the user
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Failed to add torrent",
                "detail": str(exc),
                "hint": "Check QBIT_HOST/QBIT_USER/QBIT_PASS.",
                "back_url": "/search",
                "back_label": "Back to search",
            },
            status_code=502,
        )

    with get_session(request.app.state.engine) as session:
        job = MediaJob(
            type=MediaType(media_type),
            title=title,
            year=year,
            season=season,
            episode=episode,
            episode_set=persisted_episode_set,
            release_title=release_title,
            qbit_hash=torrent_hash,
            category=category,
            status=JobStatus.QUEUED,
        )
        session.add(job)
        session.commit()

    return RedirectResponse(url="/jobs", status_code=303)


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


@router.post("/jobs/{job_id}/delete")
async def delete_job(request: Request, job_id: int):
    with try_job_lock(job_id) as acquired:
        if not acquired:
            return HTMLResponse(
                "This job is already being updated; retry shortly.", status_code=409
            )
    qbit = request.app.state.qbit

    with get_session(request.app.state.engine) as session:
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

    return RedirectResponse(url="/jobs", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
async def list_jobs(request: Request, tab: str = "active"):
    if tab not in ("active", "completed"):
        tab = "active"

    with get_session(request.app.state.engine) as session:
        all_jobs = session.exec(select(MediaJob).order_by(MediaJob.created_at.desc())).all()

    active_jobs = [job for job in all_jobs if job.status in ACTIVE_TAB_STATUSES]
    completed_jobs = [job for job in all_jobs if job.status in COMPLETED_TAB_STATUSES]
    jobs = active_jobs if tab == "active" else completed_jobs

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "tab": tab,
            "active_count": len(active_jobs),
            "completed_count": len(completed_jobs),
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
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"job": job, **_episode_set_detail_context(job)},
    )


@router.websocket("/ws/jobs/active")
async def active_jobs_ws(websocket: WebSocket):
    await websocket.accept()
    last_payload = None
    disconnect_task = asyncio.create_task(wait_for_websocket_disconnect(websocket))
    try:
        while not disconnect_task.done():
            payload = active_jobs_payload(websocket.app.state.engine)
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
    try:
        while True:
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
            }
            if payload != last_payload:
                await websocket.send_json(payload)
                last_payload = payload
                if job.status in terminal_statuses:
                    break
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass


@router.post("/jobs/{job_id}/retry")
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
            except Exception:
                session.rollback()
                raise
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
