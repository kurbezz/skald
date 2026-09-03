import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from skald.main import create_app
from skald.models import MediaSubscription, MediaType, QualityProfile


def test_quality_get_creates_and_returns_default_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-default.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/quality")

    assert response.status_code == 200
    assert response.json() == {
        "id": 1,
        "allowed_resolutions": ["1080p", "2160p"],
        "excluded_tokens": ["CAM", "TS", "TeleSync"],
        "minimum_seeders": 5,
    }
    with Session(app.state.engine) as session:
        assert session.get(QualityProfile, 1) is not None


def test_quality_post_normalizes_and_updates_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-update.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/quality",
            data={
                "allowed_resolutions": ["720p", "1080p", "4K"],
                "minimum_seeders": "12",
                "excluded_tokens": " CAM, TeleSync ",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/quality"
    with Session(app.state.engine) as session:
        profile = session.get(QualityProfile, 1)
    assert profile.allowed_resolutions == ["720p", "1080p", "2160p"]
    assert profile.minimum_seeders == 12
    assert profile.excluded_tokens == ["CAM", "TeleSync"]


@pytest.mark.parametrize(
    "data",
    [
        {"minimum_seeders": "5", "excluded_tokens": "CAM"},
        {
            "allowed_resolutions": ["1080p", "invalid"],
            "minimum_seeders": "5",
            "excluded_tokens": "CAM",
        },
        {
            "allowed_resolutions": "1080p",
            "minimum_seeders": "-1",
            "excluded_tokens": "CAM",
        },
        {
            "allowed_resolutions": "1080p",
            "minimum_seeders": "5",
            "excluded_tokens": "CAM,,TS",
        },
        {
            "allowed_resolutions": "1080p",
            "minimum_seeders": "5",
            "excluded_tokens": "CAM, cam",
        },
    ],
)
def test_quality_post_rejects_invalid_profile_values(tmp_path, monkeypatch, data):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-invalid.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.post("/quality", data=data, follow_redirects=False)

    assert response.status_code == 422


def test_quality_routes_require_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-auth.db"))
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/quality", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/quality"


def test_subscription_auto_download_toggle(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "subscription-toggle.db"))
    app = create_app()

    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            subscription = MediaSubscription(
                tmdb_id=1, type=MediaType.MOVIE, title="Movie"
            )
            session.add(subscription)
            session.commit()
            subscription_id = subscription.id

        response = client.post(
            f"/subscriptions/{subscription_id}/auto-download", follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/subscriptions"
    with Session(app.state.engine) as session:
        assert session.get(MediaSubscription, subscription_id).auto_download is True
