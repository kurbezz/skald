from collections.abc import Mapping
import re
from typing import cast

import guessit

from skald.episodes import normalize_episode_set


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
_HDR_TITLE_PATTERNS = (
    ("sdr", re.compile(r"(?<!\w)sdr(?!\w)", re.IGNORECASE)),
    ("hdr", re.compile(r"(?<!\w)hdr(?!\w)", re.IGNORECASE)),
    (
        "hdr10plus",
        re.compile(r"(?<!\w)hdr10(?:\+|plus)(?!\w)", re.IGNORECASE),
    ),
    ("hdr10", re.compile(r"(?<!\w)hdr10(?![\w+])", re.IGNORECASE)),
    ("dolby_vision", re.compile(r"(?<!\w)dv(?!\w)", re.IGNORECASE)),
    ("dolby_vision", re.compile(r"(?<!\w)dovi(?!\w)", re.IGNORECASE)),
    (
        "dolby_vision",
        re.compile(r"(?<!\w)dolbyvision(?!\w)", re.IGNORECASE),
    ),
    (
        "dolby_vision",
        re.compile(r"(?<!\w)dolby[._ -]+vision(?!\w)", re.IGNORECASE),
    ),
)


def _single_guess_value(guess: Mapping[str, object], key: str) -> str | None:
    """Return one scalar GuessIt value, rejecting ambiguous parser output."""
    value = guess.get(key)
    if isinstance(value, (list, set)):
        if len(value) != 1:
            return None
        value = next(iter(value))
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return str(value)


def _normalize_resolution(value: str | None) -> str:
    if value is None:
        return "unknown"
    return _RESOLUTION_ALIASES.get(value.casefold(), "unknown")


def _normalize_audio(
    channels: str | None, codec: str | None, profile: str | None
) -> str:
    normalized_channels = _normalize_audio_value(channels)
    normalized_codec = _normalize_audio_value(codec)
    normalized_profile = _normalize_audio_value(profile)

    if "atmos" in (normalized_codec, normalized_profile):
        return "atmos"
    return normalized_channels


def _normalize_audio_value(value: str | None) -> str:
    if value is None:
        return "unknown"
    return _AUDIO_ALIASES.get(value.casefold(), "unknown")


def _normalize_hdr(value: str | None) -> str:
    if value is None:
        return "unknown"
    return _HDR_ALIASES.get(value.casefold(), "unknown")


def _hdr_from_title(release_title: str) -> str | None:
    """Resolve only explicit, tokenized HDR labels from one release title."""
    formats = {
        format_name
        for format_name, pattern in _HDR_TITLE_PATTERNS
        if pattern.search(release_title)
    }
    if len(formats) == 1:
        return formats.pop()
    if len(formats) > 1:
        return "unknown"
    return None


def parse_release(release_title: str) -> dict[str, object]:
    guess: Mapping[str, object] = guessit.guessit(release_title)
    episode_set = normalize_episode_set(
        cast(int | list[int] | tuple[int, ...] | None, guess.get("episode"))
    )
    title_hdr = _hdr_from_title(release_title)
    return {
        "title": guess.get("title"),
        "year": guess.get("year"),
        "season": guess.get("season"),
        "episode": episode_set[0] if episode_set else None,
        "episode_set": episode_set,
        "media_type": "tv" if guess.get("type") == "episode" else "movie",
        "resolution": _normalize_resolution(_single_guess_value(guess, "screen_size")),
        "audio": _normalize_audio(
            _single_guess_value(guess, "audio_channels"),
            _single_guess_value(guess, "audio_codec"),
            _single_guess_value(guess, "audio_profile"),
        ),
        "hdr": title_hdr if title_hdr is not None else _normalize_hdr(_single_guess_value(guess, "other")),
    }
