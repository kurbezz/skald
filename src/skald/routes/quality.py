from decimal import Decimal, InvalidOperation, localcontext

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from skald.auth import SESSION_COOKIE_NAME, create_csrf_token, verify_csrf_form
from skald.db import get_session
from skald.flash import set_flash
from skald.models import MediaType, QualityProfile, _utcnow
from skald.quality import (
    QualityProfileService,
    QualityProfileValidationError,
    default_quality_profile,
)

router = APIRouter()
from skald.templating import templates
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


_GIB = 2**30
_MAX_BANDS = 16


def _gib_to_bytes(raw: str, field: str) -> str:
    """Convert a GiB decimal string to a bytes string; blank stays blank."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value < 0 or value > Decimal(2**33):
        raise QualityProfileValidationError(
            field, "Enter a size in GiB, for example 1.5"
        )
    with localcontext() as ctx:
        ctx.prec = 80
        return str(int(value * _GIB))


def _bytes_to_gib(value: object) -> str:
    """Render stored bytes as a GiB string that converts back losslessly."""
    if value is None or value == "":
        return ""
    with localcontext() as ctx:
        ctx.prec = 80
        exact = Decimal(int(value)) / Decimal(_GIB)
        rounded = exact.quantize(Decimal("0.001"))
        chosen = rounded if int(rounded * _GIB) == int(value) else exact
        return format(chosen.normalize(), "f")


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
        bands.append(
            {
                "min_bytes": _gib_to_bytes(minimum, "preferred_size_bands"),
                "max_bytes": _gib_to_bytes(maximum, "preferred_size_bands"),
            }
        )
    return bands


def _blank_optional(values: list[str]) -> list[str]:
    return [value for value in values if value.strip()]


def _form_from_profile(profile: dict[str, object], extra_bands: int) -> dict[str, object]:
    bands = [
        (_bytes_to_gib(band["min_bytes"]), _bytes_to_gib(band["max_bytes"]))
        for band in profile["preferred_size_bands"]
    ]
    bands.extend([("", "")] * (1 + extra_bands))
    return {
        "allowed_resolutions": list(profile["allowed_resolutions"]),
        "allowed_audio": list(profile["allowed_audio"]),
        "allowed_hdr": list(profile["allowed_hdr"]),
        "minimum_seeders": str(profile["minimum_seeders"]),
        "minimum_size": _bytes_to_gib(profile["minimum_size_bytes"]),
        "maximum_size": _bytes_to_gib(profile["maximum_size_bytes"]),
        "excluded_tokens": ", ".join(profile["excluded_tokens"]),
        "preferred_resolutions": ", ".join(profile["preferred_resolutions"]),
        "preferred_audio": ", ".join(profile["preferred_audio"]),
        "preferred_hdr": ", ".join(profile["preferred_hdr"]),
        "bands": bands[:_MAX_BANDS],
    }


def _form_from_submission(submitted: dict[str, object]) -> dict[str, object]:
    mins = list(submitted["preferred_size_band_min_gib"])
    maxs = list(submitted["preferred_size_band_max_gib"])
    size = max(len(mins), len(maxs))
    mins += [""] * (size - len(mins))
    maxs += [""] * (size - len(maxs))
    bands = [(a, b) for a, b in zip(mins, maxs) if a.strip() or b.strip()]
    bands.append(("", ""))
    return {
        "allowed_resolutions": submitted["allowed_resolutions"],
        "allowed_audio": submitted["allowed_audio"],
        "allowed_hdr": submitted["allowed_hdr"],
        "minimum_seeders": submitted["minimum_seeders"],
        "minimum_size": submitted["minimum_size_gib"],
        "maximum_size": submitted["maximum_size_gib"],
        "excluded_tokens": submitted["excluded_tokens"],
        "preferred_resolutions": ", ".join(submitted["preferred_resolutions"]),
        "preferred_audio": ", ".join(submitted["preferred_audio"]),
        "preferred_hdr": ", ".join(submitted["preferred_hdr"]),
        "bands": bands[:_MAX_BANDS],
    }


def _page_context(
    request: Request,
    active: str,
    *,
    submitted: dict[str, object] | None = None,
    error: QualityProfileValidationError | None = None,
    extra_bands: int = 0,
) -> dict[str, object]:
    with get_session(request.app.state.engine) as session:
        profiles = dict(_profile_views(_profiles(session)))
        session.commit()
    if submitted is not None:
        form = _form_from_submission(submitted)
    else:
        form = _form_from_profile(profiles[MediaType(active)], extra_bands)
    return {
        "active": active,
        "form": form,
        "extra_bands": extra_bands,
        "error_field": error.field if error else None,
        "error": str(error) if error else None,
        "csrf_token": create_csrf_token(request.cookies.get(SESSION_COOKIE_NAME)),
    }


@router.get("/quality", response_class=HTMLResponse)
async def get_quality(request: Request, profile: str = "movie", extra_bands: str = "0"):
    active = profile if profile in {"movie", "tv"} else "movie"
    extra = int(extra_bands) if extra_bands.isdigit() else 0
    return templates.TemplateResponse(
        request,
        "quality.html",
        _page_context(request, active, extra_bands=min(extra, _MAX_BANDS)),
    )


@router.post("/quality/{media_type}")
async def update_quality(
    request: Request,
    media_type: str,
    allowed_resolutions: list[str] = Form(default=[]),
    allowed_audio: list[str] = Form(default=[]),
    allowed_hdr: list[str] = Form(default=[]),
    minimum_seeders: str = Form(""),
    minimum_size_gib: str = Form(""),
    maximum_size_gib: str = Form(""),
    excluded_tokens: str = Form(""),
    preferred_resolutions: list[str] = Form(default=[]),
    preferred_audio: list[str] = Form(default=[]),
    preferred_hdr: list[str] = Form(default=[]),
    preferred_size_band_min_gib: list[str] = Form(default=[]),
    preferred_size_band_max_gib: list[str] = Form(default=[]),
):
    if not await verify_csrf_form(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    if media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
        raise HTTPException(status_code=404, detail="Quality profile not found")

    submitted: dict[str, object] = {
        "media_type": media_type,
        "allowed_resolutions": allowed_resolutions,
        "allowed_audio": allowed_audio,
        "allowed_hdr": allowed_hdr,
        "minimum_seeders": minimum_seeders,
        "minimum_size_gib": minimum_size_gib,
        "maximum_size_gib": maximum_size_gib,
        "excluded_tokens": excluded_tokens,
        "preferred_resolutions": _blank_optional(preferred_resolutions),
        "preferred_audio": _blank_optional(preferred_audio),
        "preferred_hdr": _blank_optional(preferred_hdr),
        "preferred_size_band_min_gib": preferred_size_band_min_gib,
        "preferred_size_band_max_gib": preferred_size_band_max_gib,
    }
    try:
        payload = {
            **submitted,
            "minimum_size_bytes": _gib_to_bytes(minimum_size_gib, "minimum_size_bytes"),
            "maximum_size_bytes": _gib_to_bytes(maximum_size_gib, "maximum_size_bytes"),
            "preferred_size_bands": _size_bands(
                preferred_size_band_min_gib, preferred_size_band_max_gib
            ),
        }
        values = _profile_service.normalize_profile_input(payload)
    except QualityProfileValidationError as exc:
        return templates.TemplateResponse(
            request,
            "quality.html",
            _page_context(request, media_type, submitted=submitted, error=exc),
            status_code=422,
        )

    with get_session(request.app.state.engine) as session:
        profile = get_or_create_profile(session, MediaType(media_type))
        for field, value in values.__dict__.items():
            setattr(profile, field, value)
        profile.updated_at = _utcnow()
        session.add(profile)
        session.commit()

    label = "Movie" if media_type == MediaType.MOVIE.value else "TV"
    return set_flash(
        RedirectResponse(url=f"/quality?profile={media_type}", status_code=303),
        f"{label} profile saved.",
    )
