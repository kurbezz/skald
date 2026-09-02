import pytest

from skald.episodes import (
    format_episode_set_input,
    format_episode_set_label,
    normalize_episode_set,
    parse_episode_set_input,
    serialize_episode_set,
)


@pytest.mark.parametrize(
    ("raw_episode", "expected"),
    [
        (5, (5,)),
        ([3, 1, 3, 2], (1, 2, 3)),
    ],
)
def test_normalize_episode_set_accepts_guessit_scalars_and_lists(
    raw_episode, expected
):
    assert normalize_episode_set(raw_episode) == expected


@pytest.mark.parametrize("raw_episode", [0, -1, [1, 0], [1, "2"]])
def test_normalize_episode_set_rejects_nonpositive_or_noninteger_values(raw_episode):
    with pytest.raises(ValueError):
        normalize_episode_set(raw_episode)


def test_normalize_episode_set_treats_missing_episode_as_empty_set():
    assert normalize_episode_set(None) == ()


def test_parse_episode_set_input_accepts_persisted_json_array():
    assert parse_episode_set_input("[3, 1, 2]") == (1, 2, 3)


def test_parse_episode_set_input_accepts_human_ranges():
    assert parse_episode_set_input("1-3,6") == (1, 2, 3, 6)


@pytest.mark.parametrize(
    "value",
    ["", "[]", "[1, 0]", "[1, \"2\"]", "1", "1-", "-1", "3-1", "1,,2", "0"],
)
def test_parse_episode_set_input_rejects_malformed_empty_or_invalid_values(value):
    with pytest.raises(ValueError):
        parse_episode_set_input(value)


def test_serialize_episode_set_uses_compact_json():
    assert serialize_episode_set((1, 2, 3)) == "[1,2,3]"


@pytest.mark.parametrize(
    ("episodes", "expected"),
    [
        ((5,), "Episode 5"),
        ((1, 2, 3), "Episodes 1-3"),
        ((1, 2, 3, 6, 8, 9), "Episodes 1-3, 6, 8-9"),
    ],
)
def test_format_episode_set_label(episodes, expected):
    assert format_episode_set_label(episodes) == expected


def test_format_episode_set_input_produces_human_editable_ranges():
    assert format_episode_set_input((1, 2, 3, 6, 8, 9)) == "1-3,6,8-9"
