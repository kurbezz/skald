from skald.indexer.base import ReleaseResult
from skald.db import get_engine, migrate_schema
from skald.models import MediaSubscription, MediaType, QualityProfile
from skald.quality import best_matching_release, default_quality_profile, profile_matches


def _release(title: str, *, seeders: int = 5) -> ReleaseResult:
    return ReleaseResult(title, "fake", 1, seeders, 0, f"magnet:?{title}")


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

    assert profile.id == 1
    assert profile.allowed_resolutions == ["1080p", "2160p"]
    assert profile.excluded_tokens == ["CAM", "TS", "TeleSync"]
    assert profile.minimum_seeders == 5
    assert subscription.auto_download is False
    assert subscription.auto_grabbed_release_id is None


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
    assert profile_columns == {
        "id", "allowed_resolutions", "excluded_tokens", "minimum_seeders", "updated_at"
    }
