from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from skald.db import get_session
from skald.models import JobStatus, MediaJob, MediaType

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")


@router.post("/grab")
async def grab(
    request: Request,
    release_title: str = Form(...),
    download_url: str = Form(...),
    media_type: str = Form(...),
    title: str = Form(...),
    year: Optional[int] = Form(None),
    season: Optional[int] = Form(None),
    episode: Optional[int] = Form(None),
):
    settings = request.app.state.settings
    qbit = request.app.state.qbit
    category = settings.category_movie if media_type == "movie" else settings.category_tv

    try:
        torrent_hash = qbit.add_torrent(download_url, category)
    except Exception as exc:  # noqa: BLE001 - surface any qBittorrent failure to the user
        return HTMLResponse(
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<title>Failed to add torrent — Skald</title>"
            "<link rel='stylesheet' href='/static/style.css'></head>"
            "<body><div class='shell'><main class='error-page'>"
            "<div class='glyph'>&times;</div>"
            "<h1>Failed to add torrent</h1>"
            f"<p><code>{exc}</code></p>"
            "<p>Check QBIT_HOST/QBIT_USER/QBIT_PASS.</p>"
            "<a class='btn' href='/search'>Back to search</a>"
            "</main></div></body></html>",
            status_code=502,
        )

    with get_session(request.app.state.engine) as session:
        job = MediaJob(
            type=MediaType(media_type),
            title=title,
            year=year,
            season=season,
            episode=episode,
            release_title=release_title,
            qbit_hash=torrent_hash,
            category=category,
            status=JobStatus.QUEUED,
        )
        session.add(job)
        session.commit()

    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/jobs/{job_id}/delete")
async def delete_job(request: Request, job_id: int):
    qbit = request.app.state.qbit

    with get_session(request.app.state.engine) as session:
        job = session.get(MediaJob, job_id)
        if job is None:
            return RedirectResponse(url="/jobs", status_code=303)

        try:
            qbit.delete_torrent(job.qbit_hash, delete_files=True)
        except Exception as exc:  # noqa: BLE001 - surface any qBittorrent failure to the user
            return HTMLResponse(
                "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                "<title>Failed to delete torrent — Skald</title>"
                "<link rel='stylesheet' href='/static/style.css'></head>"
                "<body><div class='shell'><main class='error-page'>"
                "<div class='glyph'>&times;</div>"
                "<h1>Failed to delete torrent</h1>"
                f"<p><code>{exc}</code></p>"
                "<p>Check QBIT_HOST/QBIT_USER/QBIT_PASS.</p>"
                "<a class='btn' href='/jobs'>Back to jobs</a>"
                "</main></div></body></html>",
                status_code=502,
            )

        session.delete(job)
        session.commit()

    return RedirectResponse(url="/jobs", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
async def list_jobs(request: Request):
    with get_session(request.app.state.engine) as session:
        jobs = session.exec(select(MediaJob).order_by(MediaJob.created_at.desc())).all()
    return templates.TemplateResponse(request, "jobs.html", {"jobs": jobs})


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    with get_session(request.app.state.engine) as session:
        job = session.get(MediaJob, job_id)
    return templates.TemplateResponse(request, "job_detail.html", {"job": job})


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    request: Request,
    job_id: int,
    title: str = Form(...),
    year: Optional[int] = Form(None),
    season: Optional[int] = Form(None),
    episode: Optional[int] = Form(None),
):
    with get_session(request.app.state.engine) as session:
        job = session.get(MediaJob, job_id)
        job.title = title
        job.year = year
        job.season = season
        job.episode = episode
        job.status = JobStatus.COMPLETED
        job.error_message = None
        session.add(job)
        session.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
