from skald.indexer.base import ReleaseResult
import pytest
from skald.db import get_engine, migrate_schema
from skald.models import MediaSubscription, MediaType, QualityProfile
from skald.quality import (
    QUALITY_SCORE_VERSION,
    ObservedQuality,
    QualityCandidate,
    QualityProfileService,
    QualityProfileValidationError,
    best_matching_release,
    default_quality_profile,
    profile_matches,
)


def _release(title: str, *, seeders: int = 5) -> ReleaseResult:
    return ReleaseResult(title, "fake", 1, seeders, 0, f"magnet:?{title}")


def _profile(**overrides) -> QualityProfile:
    values = {
        "media_type": MediaType.MOVIE,
        "allowed_resolutions": [],
        "allowed_audio": [],
        "allowed_hdr": [],
        "minimum_size_bytes": None,
        "maximum_size_bytes": None,
        "minimum_seeders": 0,
        "excluded_tokens": [],
        "preferred_resolutions": [],
        "preferred_audio": [],
        "preferred_hdr": [],
        "preferred_size_bands": [],
    }
    values.update(overrides)
    return QualityProfile(**values)


def test_default_profile_accepts_1080p_with_five_seeders():
    profile = default_quality_profile()

    assert profile_matches(profile, _release("Movie.2026.1080p.WEB"))


def test_default_profile_normalizes_4k_and_2160p_aliases():
    profile = default_quality_profile()

    assert profile_matches(profile, _release("Movie.2026.4K.WEB"))
    assert profile_matches(profile, _release("Movie.2026.2160p.WEB"))


def test_default_profile_rejects_low_resolution_and_too_few_seeders():
    profile = default_quality_profile()

    assert not profile_matches(profile, _release("Movie.2026.720p.WEB", seeders=99))
    assert not profile_matches(profile, _release("Movie.2026.1080p.WEB", seeders=4))


def test_profile_rejects_cam_ts_and_telesync_case_insensitively_at_word_boundaries():
    profile = default_quality_profile()

    for title in ("Movie.1080p.CAM", "Movie.4K.TS", "Movie.1080p.TeleSync"):
        assert not profile_matches(profile, _release(title, seeders=99))
    assert profile_matches(profile, _release("Movie.1080p.Cats", seeders=99))


def test_best_matching_release_ranks_by_seeders_resolution_then_title():
    profile = default_quality_profile()
    releases = [
        _release("Zulu.1080p.WEB", seeders=20),
        _release("Alpha.2160p.WEB", seeders=20),
        _release("Best.2160p.WEB", seeders=21),
        _release("Ignored.720p.WEB", seeders=100),
    ]

    assert best_matching_release(profile, releases) is releases[2]
    assert best_matching_release(profile, releases[:2]) is releases[1]


def test_profile_and_subscription_defaults_support_auto_grab():
    profile = default_quality_profile()
    subscription = MediaSubscription(tmdb_id=1, type=MediaType.MOVIE, title="Movie")

    assert profile.allowed_resolutions == ["1080p", "2160p"]
    assert profile.excluded_tokens == ["CAM", "TS", "TeleSync"]
    assert profile.minimum_seeders == 5
    assert subscription.auto_download is False
    assert subscription.auto_grabbed_release_id is None


def test_quality_profile_requires_an_explicit_media_type():
    assert QualityProfile.model_fields["media_type"].is_required()


def test_migration_adds_subscription_auto_grab_columns_and_quality_profile_table(tmp_path):
    engine = get_engine(str(tmp_path / "legacy.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE mediasubscription (id INTEGER PRIMARY KEY)")

    migrate_schema(engine)

    with engine.connect() as connection:
        subscription_columns = {
            column[1]: column
            for column in connection.exec_driver_sql("PRAGMA table_info(mediasubscription)").fetchall()
        }
        profile_columns = {
            column[1]
            for column in connection.exec_driver_sql("PRAGMA table_info(qualityprofile)").fetchall()
        }

    assert subscription_columns["auto_download"][3] == 1
    assert subscription_columns["auto_download"][4] in ("0", "FALSE")
    assert subscription_columns["auto_grabbed_release_id"][3] == 0
    assert {
        "id", "media_type", "allowed_resolutions", "allowed_audio", "allowed_hdr",
        "minimum_size_bytes", "maximum_size_bytes", "minimum_seeders", "excluded_tokens",
        "preferred_resolutions", "preferred_audio", "preferred_hdr", "preferred_size_bands",
        "updated_at",
    } <= profile_columns


def test_unknown_audio_fails_when_audio_is_hard_constrained():
    profile = _profile(allowed_audio=["atmos"])
    observed = ObservedQuality("1080p", "unknown", "sdr", 2_000_000_000)

    assert not QualityProfileService().eligible(profile, observed, title="Film", seeders=10)


def test_eligibility_applies_each_hard_dimension_and_inclusive_size_bounds():
    service = QualityProfileService()
    profile = _profile(
        allowed_resolutions=["1080p"],
        allowed_audio=["5.1"],
        allowed_hdr=["hdr"],
        minimum_size_bytes=100,
        maximum_size_bytes=200,
        minimum_seeders=3,
        excluded_tokens=["CAM"],
    )
    good = ObservedQuality("1080p", "5.1", "hdr", 100)

    assert service.eligible(profile, good, title="Film", seeders=3)
    assert service.eligible(profile, ObservedQuality("1080p", "5.1", "hdr", 200), title="Film", seeders=3)
    assert not service.eligible(profile, ObservedQuality("720p", "5.1", "hdr", 100), title="Film", seeders=3)
    assert not service.eligible(profile, ObservedQuality("1080p", "stereo", "hdr", 100), title="Film", seeders=3)
    assert not service.eligible(profile, ObservedQuality("1080p", "5.1", "sdr", 100), title="Film", seeders=3)
    assert not service.eligible(profile, ObservedQuality("1080p", "5.1", "hdr", None), title="Film", seeders=3)
    assert not service.eligible(profile, good, title="Film.CAM", seeders=3)
    assert service.eligible(profile, good, title="Film.Cameras", seeders=3)
    assert not service.eligible(profile, good, title="Film", seeders=2)


def test_observed_quality_treats_nonpositive_size_as_unknown_and_empty_hard_lists_as_open():
    service = QualityProfileService()

    observed = service.observed_from_parsed(
        {"resolution": "480p", "audio": "stereo", "hdr": "sdr"}, 0
    )

    assert observed == ObservedQuality("480p", "stereo", "sdr", None)
    assert service.eligible(_profile(), observed, title="Film", seeders=0)


def test_normalize_profile_input_validates_bands_and_preserves_preference_order():
    service = QualityProfileService()
    normalized = service.normalize_profile_input(
        {
            "allowed_resolutions": "4K, 1080p",
            "allowed_audio": ["Dolby Atmos"],
            "allowed_hdr": "Dolby Vision",
            "minimum_size_bytes": "1",
            "maximum_size_bytes": "2",
            "minimum_seeders": "0",
            "excluded_tokens": "CAM, TS",
            "preferred_resolutions": "2160p, 1080p",
            "preferred_audio": "atmos",
            "preferred_hdr": "dolby vision",
            "preferred_size_bands": [
                {"min_bytes": "20", "max_bytes": "30"},
                {"min_bytes": 1, "max_bytes": 10},
            ],
        }
    )

    assert normalized.allowed_resolutions == ["2160p", "1080p"]
    assert normalized.preferred_size_bands == [
        {"min_bytes": 20, "max_bytes": 30},
        {"min_bytes": 1, "max_bytes": 10},
    ]
    for payload, field in [
        ({"allowed_resolutions": "unknown"}, "allowed_resolutions"),
        ({"minimum_size_bytes": "3", "maximum_size_bytes": "2"}, "minimum_size_bytes"),
        ({"excluded_tokens": " "}, "excluded_tokens"),
        ({"preferred_size_bands": [{"min_bytes": 1, "max_bytes": 2}, {"min_bytes": 2, "max_bytes": 3}]}, "preferred_size_bands"),
    ]:
        try:
            service.normalize_profile_input(payload)
        except QualityProfileValidationError as exc:
            assert exc.field == field
        else:
            raise AssertionError("invalid profile input was accepted")


def test_rank_uses_preferences_then_existing_seeder_order_then_fingerprint():
    profile = _profile(preferred_resolutions=["2160p"])
    candidates = [
        QualityCandidate(_release("Zulu.1080p", seeders=10), "c", ObservedQuality("1080p", "unknown", "unknown", None)),
        QualityCandidate(_release("Alpha.2160p", seeders=5), "b", ObservedQuality("2160p", "unknown", "unknown", None)),
        QualityCandidate(_release("Beta.2160p", seeders=5), "a", ObservedQuality("2160p", "unknown", "unknown", None)),
    ]

    assert [candidate.fingerprint for candidate in QualityProfileService().rank(profile, candidates)] == ["b", "a", "c"]


def test_ranking_key_uses_each_preference_position_in_submitted_band_order():
    profile = _profile(
        preferred_resolutions=["2160p"],
        preferred_audio=["atmos"],
        preferred_hdr=["dolby_vision"],
        preferred_size_bands=[{"min_bytes": 10, "max_bytes": 20}],
    )
    preferred = QualityCandidate(
        _release("Preferred", seeders=1), "preferred", ObservedQuality("2160p", "atmos", "dolby_vision", 10)
    )
    unpreferred = QualityCandidate(
        _release("Unpreferred", seeders=99), "unpreferred", ObservedQuality("1080p", "stereo", "sdr", 21)
    )

    assert QualityProfileService().ranking_key(profile, preferred)[:4] == (0, 0, 0, 0)
    assert QualityProfileService().ranking_key(profile, unpreferred)[:4] == (1, 1, 1, 1)
    assert [candidate.fingerprint for candidate in QualityProfileService().rank(profile, [unpreferred, preferred])] == ["preferred", "unpreferred"]


def test_empty_preferences_preserve_legacy_seeder_first_order_and_fingerprint_ties():
    profile = _profile()
    candidates = [
        QualityCandidate(_release("Zulu", seeders=10), "z", ObservedQuality("1080p", "unknown", "unknown", None)),
        QualityCandidate(_release("Alpha", seeders=9), "a", ObservedQuality("2160p", "unknown", "unknown", None)),
        QualityCandidate(_release("Same", seeders=10), "b", ObservedQuality("1080p", "unknown", "unknown", None)),
        QualityCandidate(_release("Same", seeders=10), "a", ObservedQuality("1080p", "unknown", "unknown", None)),
    ]

    assert [candidate.fingerprint for candidate in QualityProfileService().rank(profile, candidates)] == ["a", "b", "z", "a"]


def test_fixed_score_is_profile_independent_and_versioned():
    service = QualityProfileService()

    assert QUALITY_SCORE_VERSION == "v1"
    assert service.fixed_score(ObservedQuality("2160p", "atmos", "dolby_vision", None)) == (4, 4, 5)


def test_profile_input_normalizes_each_supported_alias_and_rejects_unknown_values():
    service = QualityProfileService()
    normalized = service.normalize_profile_input(
        {
            "allowed_resolutions": "480p, 720p, 1080p, 4K",
            "allowed_audio": "2.0, 5.1, 7.1, Dolby Atmos",
            "allowed_hdr": "SDR, HDR, HDR10, HDR10+, DOVI",
            "excluded_tokens": "CAM",
        }
    )

    assert normalized.allowed_resolutions == ["480p", "720p", "1080p", "2160p"]
    assert normalized.allowed_audio == ["stereo", "5.1", "7.1", "atmos"]
    assert normalized.allowed_hdr == ["sdr", "hdr", "hdr10", "hdr10plus", "dolby_vision"]
    for field in ("allowed_resolutions", "allowed_audio", "allowed_hdr"):
        with pytest.raises(QualityProfileValidationError) as exc_info:
            service.normalize_profile_input({field: "not-a-quality", "excluded_tokens": "CAM"})
        assert exc_info.value.field == field


def test_observed_quality_rejects_nonfinite_parsed_values():
    observed = QualityProfileService().observed_from_parsed(
        {"resolution": "4K", "audio": "lossless", "hdr": None}, 1
    )

    assert observed == ObservedQuality("unknown", "unknown", "unknown", 1)


def test_default_profile_is_not_a_movie_singleton_when_tv_is_requested():
    profile = default_quality_profile(MediaType.TV)

    assert profile.id is None
    assert profile.media_type is MediaType.TV
