"""Pure evaluation and ordering for structured release-quality profiles."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

from skald.indexer.base import ReleaseResult
from skald.models import MediaType, QualityProfile
from skald.parser import parse_release


QUALITY_SCORE_VERSION = "v1"

_RESOLUTION_ALIASES = {
    "480p": "480p",
    "720p": "720p",
    "1080p": "1080p",
    "2160p": "2160p",
    "4k": "2160p",
}
_AUDIO_ALIASES = {
    "2.0": "stereo",
    "2ch": "stereo",
    "stereo": "stereo",
    "5.1": "5.1",
    "7.1": "7.1",
    "atmos": "atmos",
    "dolby atmos": "atmos",
}
_HDR_ALIASES = {
    "sdr": "sdr",
    "hdr": "hdr",
    "hdr10": "hdr10",
    "hdr10+": "hdr10plus",
    "hdr10plus": "hdr10plus",
    "dolby vision": "dolby_vision",
    "dovi": "dolby_vision",
}
_RESOLUTION_FIXED_RANK = {"unknown": 0, "480p": 1, "720p": 2, "1080p": 3, "2160p": 4}
_AUDIO_FIXED_RANK = {"unknown": 0, "stereo": 1, "5.1": 2, "7.1": 3, "atmos": 4}
_HDR_FIXED_RANK = {
    "unknown": 0,
    "sdr": 1,
    "hdr": 2,
    "hdr10": 3,
    "hdr10plus": 4,
    "dolby_vision": 5,
}
_MAX_INT = 2**63 - 1


@dataclass(frozen=True)
class QualityProfileValues:
    allowed_resolutions: list[str]
    allowed_audio: list[str]
    allowed_hdr: list[str]
    minimum_size_bytes: int | None
    maximum_size_bytes: int | None
    minimum_seeders: int
    excluded_tokens: list[str]
    preferred_resolutions: list[str]
    preferred_audio: list[str]
    preferred_hdr: list[str]
    preferred_size_bands: list[dict[str, int]]


@dataclass(frozen=True)
class ObservedQuality:
    resolution: str
    audio: str
    hdr: str
    size_bytes: int | None


@dataclass(frozen=True)
class QualityCandidate:
    release: ReleaseResult
    fingerprint: str
    observed: ObservedQuality


class QualityProfileValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


def default_quality_profile(media_type: MediaType = MediaType.MOVIE) -> QualityProfile:
    """Return an unsaved default profile for the requested media type."""
    return QualityProfile(media_type=media_type)


class QualityProfileService:
    """Validate, filter, and rank quality values without database I/O."""

    def normalize_profile_input(self, payload: Mapping[str, object]) -> QualityProfileValues:
        allowed_resolutions = self._normalized_values(
            payload.get("allowed_resolutions", []), "allowed_resolutions", _RESOLUTION_ALIASES
        )
        allowed_audio = self._normalized_values(
            payload.get("allowed_audio", []), "allowed_audio", _AUDIO_ALIASES
        )
        allowed_hdr = self._normalized_values(
            payload.get("allowed_hdr", []), "allowed_hdr", _HDR_ALIASES
        )
        preferred_resolutions = self._normalized_values(
            payload.get("preferred_resolutions", []), "preferred_resolutions", _RESOLUTION_ALIASES
        )
        preferred_audio = self._normalized_values(
            payload.get("preferred_audio", []), "preferred_audio", _AUDIO_ALIASES
        )
        preferred_hdr = self._normalized_values(
            payload.get("preferred_hdr", []), "preferred_hdr", _HDR_ALIASES
        )
        minimum_size_bytes = self._optional_integer(
            payload.get("minimum_size_bytes"), "minimum_size_bytes"
        )
        maximum_size_bytes = self._optional_integer(
            payload.get("maximum_size_bytes"), "maximum_size_bytes"
        )
        if (
            minimum_size_bytes is not None
            and maximum_size_bytes is not None
            and minimum_size_bytes > maximum_size_bytes
        ):
            raise QualityProfileValidationError(
                "minimum_size_bytes", "Minimum size cannot exceed maximum size"
            )
        minimum_seeders = self._integer(
            payload.get("minimum_seeders", 5), "minimum_seeders"
        )
        excluded_tokens = self._excluded_tokens(payload.get("excluded_tokens", ["CAM", "TS", "TeleSync"]))
        preferred_size_bands = self._size_bands(payload.get("preferred_size_bands", []))
        return QualityProfileValues(
            allowed_resolutions=allowed_resolutions,
            allowed_audio=allowed_audio,
            allowed_hdr=allowed_hdr,
            minimum_size_bytes=minimum_size_bytes,
            maximum_size_bytes=maximum_size_bytes,
            minimum_seeders=minimum_seeders,
            excluded_tokens=excluded_tokens,
            preferred_resolutions=preferred_resolutions,
            preferred_audio=preferred_audio,
            preferred_hdr=preferred_hdr,
            preferred_size_bands=preferred_size_bands,
        )

    def observed_from_parsed(
        self, parsed: Mapping[str, object], size_bytes: int | None
    ) -> ObservedQuality:
        return ObservedQuality(
            resolution=self._observed_value(parsed.get("resolution"), _RESOLUTION_FIXED_RANK),
            audio=self._observed_value(parsed.get("audio"), _AUDIO_FIXED_RANK),
            hdr=self._observed_value(parsed.get("hdr"), _HDR_FIXED_RANK),
            size_bytes=size_bytes if isinstance(size_bytes, int) and not isinstance(size_bytes, bool) and size_bytes > 0 else None,
        )

    def eligible(
        self, profile: QualityProfile, observed: ObservedQuality, *, title: str, seeders: int
    ) -> bool:
        if seeders < profile.minimum_seeders:
            return False
        if _has_excluded_token(title, profile.excluded_tokens):
            return False
        if profile.allowed_resolutions and observed.resolution not in profile.allowed_resolutions:
            return False
        if profile.allowed_audio and observed.audio not in profile.allowed_audio:
            return False
        if profile.allowed_hdr and observed.hdr not in profile.allowed_hdr:
            return False
        if profile.minimum_size_bytes is not None and (
            observed.size_bytes is None or observed.size_bytes < profile.minimum_size_bytes
        ):
            return False
        if profile.maximum_size_bytes is not None and (
            observed.size_bytes is None or observed.size_bytes > profile.maximum_size_bytes
        ):
            return False
        return True

    def ranking_key(
        self, profile: QualityProfile, candidate: QualityCandidate
    ) -> tuple[int, int, int, int, int, int, str, str, str, str]:
        observed = candidate.observed
        release = candidate.release
        return (
            _preference_position(profile.preferred_resolutions, observed.resolution),
            _preference_position(profile.preferred_audio, observed.audio),
            _preference_position(profile.preferred_hdr, observed.hdr),
            _size_band_position(profile.preferred_size_bands, observed.size_bytes),
            -release.seeders,
            -_RESOLUTION_FIXED_RANK.get(observed.resolution, 0),
            release.title.casefold(),
            release.indexer.casefold(),
            release.download_url,
            candidate.fingerprint,
        )

    def rank(
        self, profile: QualityProfile, candidates: Iterable[QualityCandidate]
    ) -> list[QualityCandidate]:
        return sorted(
            (
                candidate
                for candidate in candidates
                if self.eligible(
                    profile,
                    candidate.observed,
                    title=candidate.release.title,
                    seeders=candidate.release.seeders,
                )
            ),
            key=lambda candidate: self.ranking_key(profile, candidate),
        )

    def fixed_score(self, observed: ObservedQuality) -> tuple[int, int, int]:
        return (
            _RESOLUTION_FIXED_RANK.get(observed.resolution, 0),
            _AUDIO_FIXED_RANK.get(observed.audio, 0),
            _HDR_FIXED_RANK.get(observed.hdr, 0),
        )

    @staticmethod
    def _observed_value(value: object, ranks: Mapping[str, int]) -> str:
        return value if isinstance(value, str) and value in ranks else "unknown"

    def _normalized_values(
        self, raw: object, field: str, aliases: Mapping[str, str]
    ) -> list[str]:
        values = _form_values(raw, field)
        normalized: list[str] = []
        for value in values:
            canonical = aliases.get(value.casefold())
            if canonical is None:
                raise QualityProfileValidationError(field, "Unknown quality value")
            if canonical in normalized:
                raise QualityProfileValidationError(field, "Quality values must be unique")
            normalized.append(canonical)
        return normalized

    def _optional_integer(self, raw: object, field: str) -> int | None:
        if raw is None or raw == "":
            return None
        return self._integer(raw, field)

    @staticmethod
    def _integer(raw: object, field: str) -> int:
        if isinstance(raw, bool):
            raise QualityProfileValidationError(field, "Must be an integer")
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            value = int(raw.strip())
        else:
            raise QualityProfileValidationError(field, "Must be an integer")
        if not 0 <= value <= _MAX_INT:
            raise QualityProfileValidationError(field, "Integer is out of range")
        return value

    def _excluded_tokens(self, raw: object) -> list[str]:
        values = _form_values(raw, "excluded_tokens")
        if not 1 <= len(values) <= 32:
            raise QualityProfileValidationError("excluded_tokens", "Provide 1 to 32 excluded tokens")
        seen: set[str] = set()
        for value in values:
            if len(value) > 64 or not value.strip():
                raise QualityProfileValidationError("excluded_tokens", "Excluded token is invalid")
            key = value.casefold()
            if key in seen:
                raise QualityProfileValidationError("excluded_tokens", "Excluded tokens must be unique")
            seen.add(key)
        return values

    def _size_bands(self, raw: object) -> list[dict[str, int]]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise QualityProfileValidationError(
                    "preferred_size_bands", "Size bands must be a list"
                ) from exc
        if not isinstance(raw, (list, tuple)) or len(raw) > 16:
            raise QualityProfileValidationError(
                "preferred_size_bands", "Provide 0 to 16 size bands"
            )
        bands: list[dict[str, int]] = []
        for band in raw:
            if not isinstance(band, Mapping):
                raise QualityProfileValidationError("preferred_size_bands", "Size band is invalid")
            minimum = self._integer(band.get("min_bytes"), "preferred_size_bands")
            maximum = self._integer(band.get("max_bytes"), "preferred_size_bands")
            if minimum > maximum:
                raise QualityProfileValidationError("preferred_size_bands", "Size band is invalid")
            bands.append({"min_bytes": minimum, "max_bytes": maximum})
        ordered = sorted(bands, key=lambda band: band["min_bytes"])
        for left, right in zip(ordered, ordered[1:]):
            if left["min_bytes"] <= right["max_bytes"] and right["min_bytes"] <= left["max_bytes"]:
                raise QualityProfileValidationError("preferred_size_bands", "Size bands overlap")
        return bands


def _form_values(raw: object, field: str) -> list[str]:
    if isinstance(raw, str):
        raw_values: Iterable[object] = [raw]
    elif isinstance(raw, (list, tuple)):
        raw_values = raw
    else:
        raise QualityProfileValidationError(field, "Must be a string or list")
    values: list[str] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            raise QualityProfileValidationError(field, "Must contain strings")
        values.extend(value.strip() for value in raw_value.split(","))
    if any(not value for value in values):
        raise QualityProfileValidationError(field, "Values cannot be blank")
    return values


def _has_excluded_token(title: str, excluded_tokens: Iterable[str]) -> bool:
    folded_title = title.casefold()
    for token in excluded_tokens:
        folded_token = token.casefold()
        if not folded_token:
            continue
        start = folded_title.find(folded_token)
        while start != -1:
            end = start + len(folded_token)
            before_is_word = start > 0 and _is_word_char(folded_title[start - 1])
            after_is_word = end < len(folded_title) and _is_word_char(folded_title[end])
            if not before_is_word and not after_is_word:
                return True
            start = folded_title.find(folded_token, start + 1)
    return False


def _is_word_char(value: str) -> bool:
    return value == "_" or value.isalnum()


def _preference_position(preferences: list[str], value: str) -> int:
    try:
        return preferences.index(value)
    except ValueError:
        return len(preferences)


def _size_band_position(bands: list[dict[str, int]], size_bytes: int | None) -> int:
    if size_bytes is not None:
        for position, band in enumerate(bands):
            if band["min_bytes"] <= size_bytes <= band["max_bytes"]:
                return position
    return len(bands)


# Compatibility adapters keep existing callers on the structured parser/service
# until their scanner integration is updated.
def profile_matches(profile: QualityProfile, release: ReleaseResult) -> bool:
    parsed = parse_release(release.title)
    observed = QualityProfileService().observed_from_parsed(parsed, release.size_bytes)
    return QualityProfileService().eligible(
        profile, observed, title=release.title, seeders=release.seeders
    )


def best_matching_release(
    profile: QualityProfile, releases: Iterable[ReleaseResult]
) -> ReleaseResult | None:
    service = QualityProfileService()
    candidates = [
        QualityCandidate(
            release=release,
            fingerprint=release.download_url,
            observed=service.observed_from_parsed(parse_release(release.title), release.size_bytes),
        )
        for release in releases
    ]
    ranked = service.rank(profile, candidates)
    return ranked[0].release if ranked else None
