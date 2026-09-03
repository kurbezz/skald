import skald.parser as parser
import pytest


parse_release = parser.parse_release


def test_parse_movie_release():
    guess = parse_release("The.Matrix.1999.1080p.BluRay.x264-GROUP")
    assert guess["title"] == "The Matrix"
    assert guess["year"] == 1999
    assert guess["media_type"] == "movie"
    assert guess["season"] is None
    assert guess["episode"] is None
    assert guess["episode_set"] == ()


def test_parse_tv_release():
    guess = parse_release("Breaking.Bad.S01E05.720p.HDTV.x264-GROUP")
    assert guess["title"] == "Breaking Bad"
    assert guess["season"] == 1
    assert guess["episode"] == 5
    assert guess["episode_set"] == (5,)
    assert guess["media_type"] == "tv"


def test_parse_multi_episode_release_preserves_first_episode_and_set():
    guess = parse_release("Breaking.Bad.S01E03-E05.720p.HDTV.x264-GROUP")
    assert guess["episode"] == 3
    assert guess["episode_set"] == (3, 4, 5)


def test_parse_release_returns_only_normalized_known_quality_values(monkeypatch):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {
            "title": "Film",
            "screen_size": "4K",
            "audio_channels": "7.1",
            "audio_profile": "Dolby Atmos",
            "other": "Dolby Vision",
        },
    )

    parsed = parse_release("Film.4K.DV.Atmos.7.1")

    assert (parsed["resolution"], parsed["audio"], parsed["hdr"]) == (
        "2160p",
        "atmos",
        "dolby_vision",
    )


def test_parse_release_returns_unknown_for_unsupported_or_conflicting_quality_values(monkeypatch):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {
            "title": "Film",
            "screen_size": ["1080p", "2160p"],
            "audio_channels": "lossless",
            "audio_codec": "stereo",
            "other": "HLG",
        },
    )

    parsed = parse_release("Film")

    assert (parsed["resolution"], parsed["audio"], parsed["hdr"]) == (
        "unknown",
        "unknown",
        "unknown",
    )


def test_parse_release_normalizes_finite_quality_aliases(monkeypatch):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {
            "title": "Film",
            "screen_size": "720p",
            "audio_channels": "2ch",
            "other": "HDR10+",
        },
    )

    parsed = parse_release("Film")

    assert (parsed["resolution"], parsed["audio"], parsed["hdr"]) == (
        "720p",
        "stereo",
        "hdr10plus",
    )


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        ("SDR", "sdr"),
        (["HDR"], "hdr"),
        ({"HDR10"}, "hdr10"),
        ("HDR10+", "hdr10plus"),
        (["Dolby Vision"], "dolby_vision"),
    ],
)
def test_parse_release_normalizes_guessit_other_hdr_values(monkeypatch, other, expected):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {"title": "Film", "other": other},
    )

    assert parse_release("Film")["hdr"] == expected


@pytest.mark.parametrize("other", ["HLG", ["HDR", "HDR10"], {"HDR10", "Dolby Vision"}])
def test_parse_release_rejects_unknown_or_conflicting_guessit_other_hdr_values(monkeypatch, other):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {"title": "Film", "other": other},
    )

    assert parse_release("Film")["hdr"] == "unknown"


def test_parse_release_requires_audio_channels_except_for_atmos(monkeypatch):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {
            "title": "Film",
            "audio_codec": "5.1",
            "audio_profile": "stereo",
        },
    )
    assert parse_release("Film")["audio"] == "unknown"

    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {"title": "Film", "audio_codec": "Dolby Atmos"},
    )
    assert parse_release("Film")["audio"] == "atmos"

    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {
            "title": "Film",
            "audio_channels": "5.1",
            "audio_codec": "stereo",
        },
    )
    assert parse_release("Film")["audio"] == "5.1"


@pytest.mark.parametrize(
    ("title", "guess", "expected"),
    [
        ("Film.2026.SDR", {"other": "Standard Dynamic Range"}, "sdr"),
        ("Film.2026.HDR", {"other": "HDR10"}, "hdr"),
        ("Film.2026.HDR10", {"other": "HDR10"}, "hdr10"),
        ("Film.2026.HDR10+", {"other": "HDR10"}, "hdr10plus"),
        ("Film.2026.HDR10Plus", {}, "hdr10plus"),
        ("Film.2026.DV", {"other": "HDR10"}, "dolby_vision"),
        ("Film.2026.Dolby.Vision", {"other": "HDR10"}, "dolby_vision"),
    ],
)
def test_parse_release_prefers_unambiguous_hdr_title_tokens_over_lossy_guessit_other(
    monkeypatch, title, guess, expected
):
    monkeypatch.setattr(parser.guessit, "guessit", lambda _title: {"title": "Film", **guess})

    assert parse_release(title)["hdr"] == expected


def test_parse_release_rejects_conflicting_hdr_title_tokens(monkeypatch):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {"title": "Film", "other": "HDR10"},
    )

    assert parse_release("Film.2026.HDR10.DV")["hdr"] == "unknown"


@pytest.mark.parametrize("title", ["Film.2026.HDR10.DOVI", "Film.2026.HDR10+.DOVI", "Film.2026.HDR10.DolbyVision"])
def test_parse_release_rejects_dovi_and_dolbyvision_dual_format_tokens(monkeypatch, title):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {"title": "Film", "other": "HDR10"},
    )

    assert parse_release(title)["hdr"] == "unknown"


@pytest.mark.parametrize("title", ["Film.2026.DOVI", "Film.2026.DolbyVision"])
def test_parse_release_recognizes_bounded_dovi_and_dolbyvision_tokens(monkeypatch, title):
    monkeypatch.setattr(
        parser.guessit,
        "guessit",
        lambda _title: {"title": "Film", "other": "HDR10"},
    )

    assert parse_release(title)["hdr"] == "dolby_vision"
