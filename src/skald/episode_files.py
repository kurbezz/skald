"""Shared resolver mapping a torrent/library file path to season + episodes."""

import re
from pathlib import PurePath

EXPLICIT_MARKER = re.compile(r"s(\d{2})[._-]?e(\d{2})(?!\d)", re.IGNORECASE)
EPISODE_ONLY_MARKER = re.compile(r"(?<![A-Za-z0-9])e(\d{2})(?!\d)", re.IGNORECASE)
_WORD_BEFORE = re.compile(
    r"(?<!\w)(?:episode|ep|серия|эпизод)[\s._-]*(\d{1,3})(?!\d)", re.IGNORECASE
)
_WORD_AFTER = re.compile(r"(?<!\w)(\d{1,3})[\s._-]*(?:серия|серии|эпизод)(?!\w)", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"^(\d{1,3})(?=[.\s_\-)])")
_SAMPLE_DIRS = {"sample", "samples"}

_EPISODE_ONLY_RULES = (EPISODE_ONLY_MARKER, _WORD_BEFORE, _WORD_AFTER)


class AmbiguousEpisodeMarkers(ValueError):
    """The file name carries conflicting episode markers."""


def resolve_file_episodes(
    file_path: str, default_season: int | None
) -> tuple[int | None, tuple[int, ...]]:
    """Return ``(season, episodes)`` for a file, or ``(None, ())`` if unknown.

    Explicit ``SxxEyy`` markers always win. Season-less rules (``Exx``, episode
    words, a leading episode number) only apply when ``default_season`` is
    known, i.e. the release is a single-season pack. Conflicting markers raise
    :class:`AmbiguousEpisodeMarkers`.
    """
    path = PurePath(file_path)
    name = path.name

    explicit = list(EXPLICIT_MARKER.finditer(name))
    if len(explicit) > 1:
        raise AmbiguousEpisodeMarkers(f"Ambiguous episode markers in {name}")
    if explicit:
        return int(explicit[0].group(1)), (int(explicit[0].group(2)),)

    if default_season is None:
        return None, ()

    for rule in _EPISODE_ONLY_RULES:
        found = {int(match.group(1)) for match in rule.finditer(name)}
        if len(found) > 1:
            raise AmbiguousEpisodeMarkers(f"Ambiguous episode markers in {name}")
        if found:
            return default_season, (found.pop(),)

    if not any(part.lower() in _SAMPLE_DIRS for part in path.parts[:-1]):
        leading = _LEADING_NUMBER.match(name)
        if leading:
            return default_season, (int(leading.group(1)),)
    return None, ()
