from skald.config import Settings


def test_settings_defaults(monkeypatch):
    for key in [
        "JACKETT_URL", "JACKETT_API_KEY", "QBIT_HOST", "QBIT_USER", "QBIT_PASS",
        "MOVIES_LIBRARY_PATH", "TV_LIBRARY_PATH", "DB_PATH",
        "TMDB_READ_ACCESS_TOKEN", "SUBSCRIPTION_CHECK_INTERVAL_SECONDS",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "SMTP_PORT",
        "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO",
    ]:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.qbit_host == "http://localhost:8080"
    assert settings.category_movie == "skald-movie"
    assert settings.category_tv == "skald-tv"
    assert settings.worker_poll_interval_seconds == 10
    assert settings.tmdb_read_access_token == ""
    assert settings.subscription_check_interval_seconds == 21_600
    assert settings.telegram_bot_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.smtp_host == ""
    assert settings.smtp_port == 587
    assert settings.smtp_username == ""
    assert settings.smtp_password == ""
    assert settings.smtp_from == ""
    assert settings.smtp_to == ""


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


def test_settings_reads_all_notification_environment_values(monkeypatch):
    values = {
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_CHAT_ID": "chat-id",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "465",
        "SMTP_USERNAME": "smtp-user",
        "SMTP_PASSWORD": "smtp-password",
        "SMTP_FROM": "sender@example.test",
        "SMTP_TO": "recipient@example.test",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.telegram_bot_token == values["TELEGRAM_BOT_TOKEN"]
    assert settings.telegram_chat_id == values["TELEGRAM_CHAT_ID"]
    assert settings.smtp_host == values["SMTP_HOST"]
    assert settings.smtp_port == 465
    assert settings.smtp_username == values["SMTP_USERNAME"]
    assert settings.smtp_password == values["SMTP_PASSWORD"]
    assert settings.smtp_from == values["SMTP_FROM"]
    assert settings.smtp_to == values["SMTP_TO"]
