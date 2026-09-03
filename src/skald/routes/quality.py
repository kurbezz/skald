from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from skald.auth import SESSION_COOKIE_NAME, create_csrf_token, verify_csrf_form
from skald.db import get_session
from skald.models import MediaType, QualityProfile, _utcnow
from skald.quality import (
    QualityProfileService,
    QualityProfileValidationError,
    default_quality_profile,
)

router = APIRouter()
templates = Jinja2Templates(directory="src/skald/templates")
_profile_service = QualityProfileService()


def get_or_create_profile(session: Session, media_type: MediaType) -> QualityProfile:
    """Return one profile for a media type, creating its compatible default if needed."""
    profile = session.exec(
        select(QualityProfile).where(QualityProfile.media_type == media_type)
    ).one_or_none()
    if profile is None:
        profile = default_quality_profile(media_type)
        session.add(profile)
        session.flush()
    return profile


def _profiles(session: Session) -> list[tuple[MediaType, QualityProfile]]:
    return [
        (MediaType.MOVIE, get_or_create_profile(session, MediaType.MOVIE)),
        (MediaType.TV, get_or_create_profile(session, MediaType.TV)),
    ]


def _profile_views(
    profiles: list[tuple[MediaType, QualityProfile]],
) -> list[tuple[MediaType, dict[str, object]]]:
    """Detach template data before committing the session that created defaults."""
    return [(media_type, profile.model_dump()) for media_type, profile in profiles]


def _submitted_payload(
    media_type: str,
    *,
    allowed_resolutions: list[str],
    allowed_audio: list[str],
    allowed_hdr: list[str],
    minimum_seeders: str,
    minimum_size_bytes: str,
    maximum_size_bytes: str,
    excluded_tokens: str,
    preferred_resolutions: list[str],
    preferred_audio: list[str],
    preferred_hdr: list[str],
    preferred_size_band_min: list[str],
    preferred_size_band_max: list[str],
) -> dict[str, object]:
    return {
        "media_type": media_type,
        "allowed_resolutions": allowed_resolutions,
        "allowed_audio": allowed_audio,
        "allowed_hdr": allowed_hdr,
        "minimum_seeders": minimum_seeders,
        "minimum_size_bytes": minimum_size_bytes,
        "maximum_size_bytes": maximum_size_bytes,
        "excluded_tokens": excluded_tokens,
        "preferred_resolutions": preferred_resolutions,
        "preferred_audio": preferred_audio,
        "preferred_hdr": preferred_hdr,
        "preferred_size_band_min": preferred_size_band_min,
        "preferred_size_band_max": preferred_size_band_max,
    }


def _size_bands(minimums: list[str], maximums: list[str]) -> list[dict[str, str]]:
    if len(minimums) != len(maximums):
        raise QualityProfileValidationError(
            "preferred_size_bands", "Each size band needs both a minimum and maximum"
        )

    bands: list[dict[str, str]] = []
    for minimum, maximum in zip(minimums, maximums):
        minimum = minimum.strip()
        maximum = maximum.strip()
        if not minimum and not maximum:
            continue
        if not minimum or not maximum:
            raise QualityProfileValidationError(
                "preferred_size_bands", "Each size band needs both a minimum and maximum"
            )
        bands.append({"min_bytes": minimum, "max_bytes": maximum})
    return bands


def _error_response(
    request: Request, submitted: dict[str, object], error: QualityProfileValidationError
) -> HTMLResponse:
    with get_session(request.app.state.engine) as session:
        profiles = _profile_views(_profiles(session))
        session.commit()
    return templates.TemplateResponse(
        request,
        "quality.html",
        {
            "profiles": profiles,
            "submitted": submitted,
            "error_field": error.field,
            "error": str(error),
            "csrf_token": create_csrf_token(request.cookies.get(SESSION_COOKIE_NAME)),
        },
        status_code=422,
    )


@router.get("/quality", response_class=HTMLResponse)
async def get_quality(request: Request):
    with get_session(request.app.state.engine) as session:
        profiles = _profile_views(_profiles(session))
        session.commit()
    return templates.TemplateResponse(
        request,
        "quality.html",
        {
            "profiles": profiles,
            "csrf_token": create_csrf_token(request.cookies.get(SESSION_COOKIE_NAME)),
        },
    )


@router.post("/quality/{media_type}")
async def update_quality(
    request: Request,
    media_type: str,
    allowed_resolutions: list[str] = Form(default=[]),
    allowed_audio: list[str] = Form(default=[]),
    allowed_hdr: list[str] = Form(default=[]),
    minimum_seeders: str = Form(""),
    minimum_size_bytes: str = Form(""),
    maximum_size_bytes: str = Form(""),
    excluded_tokens: str = Form(""),
    preferred_resolutions: list[str] = Form(default=[]),
    preferred_audio: list[str] = Form(default=[]),
    preferred_hdr: list[str] = Form(default=[]),
    preferred_size_band_min: list[str] = Form(default=[]),
    preferred_size_band_max: list[str] = Form(default=[]),
):
    if not await verify_csrf_form(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    if media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
        raise HTTPException(status_code=404, detail="Quality profile not found")

    submitted = _submitted_payload(
        media_type,
        allowed_resolutions=allowed_resolutions,
        allowed_audio=allowed_audio,
        allowed_hdr=allowed_hdr,
        minimum_seeders=minimum_seeders,
        minimum_size_bytes=minimum_size_bytes,
        maximum_size_bytes=maximum_size_bytes,
        excluded_tokens=excluded_tokens,
        preferred_resolutions=preferred_resolutions,
        preferred_audio=preferred_audio,
        preferred_hdr=preferred_hdr,
        preferred_size_band_min=preferred_size_band_min,
        preferred_size_band_max=preferred_size_band_max,
    )
    try:
        payload = {
            **submitted,
            "preferred_size_bands": _size_bands(
                preferred_size_band_min, preferred_size_band_max
            ),
        }
        values = _profile_service.normalize_profile_input(payload)
    except QualityProfileValidationError as exc:
        return _error_response(request, submitted, exc)

    with get_session(request.app.state.engine) as session:
        profile = get_or_create_profile(session, MediaType(media_type))
        for field, value in values.__dict__.items():
            setattr(profile, field, value)
        profile.updated_at = _utcnow()
        session.add(profile)
        session.commit()

    return RedirectResponse(url="/quality", status_code=303)
