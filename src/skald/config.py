import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # If not set via env/`.env`, a random key is generated per process start.
    # This is fine for a single self-hosted instance, but it means every
    # restart invalidates existing login sessions (users are logged out).
    # Set SECRET_KEY explicitly to keep sessions alive across restarts.
    secret_key: str = secrets.token_hex(32)

    jackett_url: str = "http://localhost:9117"
    jackett_api_key: str = ""

    qbit_host: str = "http://localhost:8080"
    qbit_user: str = "admin"
    qbit_pass: str = "adminadmin"

    auth_username: str = ""
    auth_password: str = ""

    movies_library_path: str = "/library/movies"
    tv_library_path: str = "/library/tv"

    db_path: str = "skald.db"

    category_movie: str = "skald-movie"
    category_tv: str = "skald-tv"

    worker_poll_interval_seconds: int = 10


def get_settings() -> Settings:
    return Settings()
