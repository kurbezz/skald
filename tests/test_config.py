from skald.config import Settings


def test_settings_defaults(monkeypatch):
    for key in [
        "JACKETT_URL", "JACKETT_API_KEY", "QBIT_HOST", "QBIT_USER", "QBIT_PASS",
        "MOVIES_LIBRARY_PATH", "TV_LIBRARY_PATH", "DB_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.qbit_host == "http://localhost:8080"
    assert settings.category_movie == "skald-movie"
    assert settings.category_tv == "skald-tv"
    assert settings.worker_poll_interval_seconds == 10


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("QBIT_HOST", "http://qbit.local:9090")
    settings = Settings(_env_file=None)
    assert settings.qbit_host == "http://qbit.local:9090"
