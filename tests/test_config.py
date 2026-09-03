from skald.config import Settings


def test_settings_defaults(monkeypatch):
    for key in [
        "JACKETT_URL", "JACKETT_API_KEY", "QBIT_HOST", "QBIT_USER", "QBIT_PASS",
        "MOVIES_LIBRARY_PATH", "TV_LIBRARY_PATH", "DB_PATH",
        "TMDB_READ_ACCESS_TOKEN", "SUBSCRIPTION_CHECK_INTERVAL_SECONDS",
    ]:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.qbit_host == "http://localhost:8080"
    assert settings.category_movie == "skald-movie"
    assert settings.category_tv == "skald-tv"
    assert settings.worker_poll_interval_seconds == 10
    assert settings.tmdb_read_access_token == ""
    assert settings.subscription_check_interval_seconds == 21_600


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("QBIT_HOST", "http://qbit.local:9090")
    settings = Settings(_env_file=None)
    assert settings.qbit_host == "http://qbit.local:9090"


def test_settings_reads_subscription_values(monkeypatch):
    monkeypatch.setenv("TMDB_READ_ACCESS_TOKEN", "tmdb-token")
    monkeypatch.setenv("SUBSCRIPTION_CHECK_INTERVAL_SECONDS", "21600")

    settings = Settings(_env_file=None)

    assert settings.tmdb_read_access_token == "tmdb-token"
    assert settings.subscription_check_interval_seconds == 21_600
