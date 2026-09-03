from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from skald.db import get_session
from skald.models import QualityProfile, _utcnow
from skald.quality import default_quality_profile

router = APIRouter()

_RESOLUTION_ALIASES = {
    "720p": "720p",
    "1080p": "1080p",
    "2160p": "2160p",
    "4k": "2160p",
}


def _profile_payload(profile: QualityProfile) -> dict:
    return {
        "id": profile.id,
        "allowed_resolutions": profile.allowed_resolutions,
        "excluded_tokens": profile.excluded_tokens,
        "minimum_seeders": profile.minimum_seeders,
    }


def _get_or_create_profile(session) -> QualityProfile:
    profile = session.get(QualityProfile, 1)
    if profile is None:
        profile = default_quality_profile()
        session.add(profile)
        session.commit()
    return profile


def _normalize_resolutions(resolutions: list[str]) -> list[str]:
    normalized = []
    for resolution in resolutions:
        value = resolution.strip().casefold()
        if value not in _RESOLUTION_ALIASES:
            raise HTTPException(status_code=422, detail="Invalid allowed resolution")
        canonical = _RESOLUTION_ALIASES[value]
        if canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        raise HTTPException(status_code=422, detail="Choose at least one allowed resolution")
    return normalized


def _normalize_excluded_tokens(tokens: str) -> list[str]:
    normalized = []
    seen = set()
    for token in tokens.split(","):
        value = token.strip()
        if not value:
            raise HTTPException(status_code=422, detail="Excluded tokens cannot be blank")
        key = value.casefold()
        if key in seen:
            raise HTTPException(status_code=422, detail="Excluded tokens must be unique")
        seen.add(key)
        normalized.append(value)
    if not normalized:
        raise HTTPException(status_code=422, detail="Provide at least one excluded token")
    return normalized


@router.get("/quality")
async def get_quality(request: Request):
    with get_session(request.app.state.engine) as session:
        profile = _get_or_create_profile(session)
        return _profile_payload(profile)


@router.post("/quality")
async def update_quality(
    request: Request,
    allowed_resolutions: list[str] = Form(...),
    minimum_seeders: str = Form(...),
    excluded_tokens: str = Form(...),
):
    resolutions = _normalize_resolutions(allowed_resolutions)
    exclusions = _normalize_excluded_tokens(excluded_tokens)
    try:
        seeders = int(minimum_seeders)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Minimum seeders must be an integer") from exc
    if seeders < 0:
        raise HTTPException(status_code=422, detail="Minimum seeders must be non-negative")

    with get_session(request.app.state.engine) as session:
        profile = _get_or_create_profile(session)
        profile.allowed_resolutions = resolutions
        profile.excluded_tokens = exclusions
        profile.minimum_seeders = seeders
        profile.updated_at = _utcnow()
        session.add(profile)
        session.commit()

    return RedirectResponse(url="/quality", status_code=303)
