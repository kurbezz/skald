"""Episode-set parsing, normalization, and presentation helpers."""

import json


def normalize_episode_set(raw_episode: int | list[int] | tuple[int, ...] | None) -> tuple[int, ...]:
    """Return positive episode numbers as a sorted, unique tuple."""
    if raw_episode is None:
        return ()
    if isinstance(raw_episode, int) and not isinstance(raw_episode, bool):
        episodes = [raw_episode]
    elif isinstance(raw_episode, (list, tuple)):
        episodes = raw_episode
    else:
        raise ValueError("episode values must be integers")

    if not episodes or any(
        not isinstance(episode, int)
        or isinstance(episode, bool)
        or episode <= 0
        for episode in episodes
    ):
        raise ValueError("episode values must be positive integers")
    return tuple(sorted(set(episodes)))


def parse_episode_set_input(value: str) -> tuple[int, ...]:
    """Parse a persisted JSON array or human-entered episode ranges."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("episode input is required")

    value = value.strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _parse_human_episode_set(value)

    if not isinstance(parsed, list):
        raise ValueError("persisted episode input must be a JSON array")
    return normalize_episode_set(parsed)


def serialize_episode_set(episodes: int | list[int] | tuple[int, ...]) -> str:
    """Serialize an episode set in compact persisted JSON form."""
    return json.dumps(normalize_episode_set(episodes), separators=(",", ":"))


def deserialize_episode_set(value: str | None) -> tuple[int, ...]:
    """Deserialize an optional persisted episode set."""
    return () if value is None else parse_episode_set_input(value)


def format_episode_label(episodes: int | list[int] | tuple[int, ...]) -> str:
    """Format episode numbers for a TV filename."""
    normalized = normalize_episode_set(episodes)
    if not normalized:
        raise ValueError("at least one episode is required")

    groups = []
    start = end = normalized[0]
    for episode in normalized[1:]:
        if episode == end + 1:
            end = episode
            continue
        groups.append((start, end))
        start = end = episode
    groups.append((start, end))
    return "-".join(
        f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}"
        for start, end in groups
    )


def format_episode_set_label(episodes: int | list[int] | tuple[int, ...]) -> str:
    """Format a display label for one or more episodes."""
    normalized = normalize_episode_set(episodes)
    if not normalized:
        raise ValueError("at least one episode is required")
    prefix = "Episode" if len(normalized) == 1 else "Episodes"
    return f"{prefix} {format_episode_set_input(normalized).replace(',', ', ')}"


def format_episode_set_input(episodes: int | list[int] | tuple[int, ...]) -> str:
    """Format episodes as editable comma-separated numbers and ranges."""
    normalized = normalize_episode_set(episodes)
    if not normalized:
        raise ValueError("at least one episode is required")

    groups = []
    start = end = normalized[0]
    for episode in normalized[1:]:
        if episode == end + 1:
            end = episode
            continue
        groups.append(_format_episode_range(start, end))
        start = end = episode
    groups.append(_format_episode_range(start, end))
    return ",".join(groups)


def _parse_human_episode_set(value: str) -> tuple[int, ...]:
    episodes = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ValueError("episode entries cannot be empty")
        bounds = part.split("-")
        if len(bounds) == 1:
            episodes.append(_parse_positive_integer(bounds[0]))
        elif len(bounds) == 2:
            start = _parse_positive_integer(bounds[0])
            end = _parse_positive_integer(bounds[1])
            if start > end:
                raise ValueError("episode ranges cannot be reversed")
            episodes.extend(range(start, end + 1))
        else:
            raise ValueError("episode range is malformed")
    return normalize_episode_set(episodes)


def _parse_positive_integer(value: str) -> int:
    value = value.strip()
    if not value.isdigit():
        raise ValueError("episode values must be positive integers")
    episode = int(value)
    if episode <= 0:
        raise ValueError("episode values must be positive integers")
    return episode


def _format_episode_range(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"
