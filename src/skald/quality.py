"""Pure helpers for evaluating the global release-quality profile."""

from collections.abc import Iterable
import re

from skald.indexer.base import ReleaseResult
from skald.models import QualityProfile


_RESOLUTION_ALIASES = {
    "1080p": "1080p",
    "2160p": "2160p",
    "4k": "2160p",
}
_RESOLUTION_RANK = {"1080p": 1080, "2160p": 2160}


def default_quality_profile() -> QualityProfile:
    """Return the default singleton profile for initial creation or evaluation."""
    return QualityProfile(id=1)


def _normalized_resolutions(resolutions: Iterable[str]) -> set[str]:
    return {
        _RESOLUTION_ALIASES[resolution.casefold()]
        for resolution in resolutions
        if resolution.casefold() in _RESOLUTION_ALIASES
    }


def _release_resolutions(title: str) -> set[str]:
    resolutions = set()
    if re.search(r"(?<!\w)1080p(?!\w)", title, flags=re.IGNORECASE):
        resolutions.add("1080p")
    if re.search(r"(?<!\w)(?:2160p|4k)(?!\w)", title, flags=re.IGNORECASE):
        resolutions.add("2160p")
    return resolutions


def _has_excluded_token(title: str, excluded_tokens: Iterable[str]) -> bool:
    return any(
        token and re.search(
            rf"(?<!\w){re.escape(token)}(?!\w)", title, flags=re.IGNORECASE
        )
        for token in excluded_tokens
    )


def profile_matches(profile: QualityProfile, release: ReleaseResult) -> bool:
    """Whether a release satisfies the profile without mutating either input."""
    if release.seeders < profile.minimum_seeders:
        return False
    if _has_excluded_token(release.title, profile.excluded_tokens):
        return False
    return bool(_release_resolutions(release.title) & _normalized_resolutions(profile.allowed_resolutions))


def best_matching_release(
    profile: QualityProfile, releases: Iterable[ReleaseResult]
) -> ReleaseResult | None:
    """Select the matching release with the best deterministic quality ordering."""
    matches = [release for release in releases if profile_matches(profile, release)]
    if not matches:
        return None

    def ranking(release: ReleaseResult) -> tuple[int, int, str, str, str]:
        resolution = max(
            (_RESOLUTION_RANK[value] for value in _release_resolutions(release.title)),
            default=0,
        )
        return (-release.seeders, -resolution, release.title.casefold(), release.indexer.casefold(), release.download_url)

    return min(matches, key=ranking)
