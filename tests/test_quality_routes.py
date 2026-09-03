import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from skald.main import create_app
from skald.models import MediaSubscription, MediaType, QualityProfile


def _profile(session, media_type):
    return session.exec(
        select(QualityProfile).where(QualityProfile.media_type == media_type)
    ).one()


def _quality_form(**overrides):
    values = {
        "allowed_resolutions": ["1080p"],
        "allowed_audio": [],
        "allowed_hdr": [],
        "minimum_seeders": "5",
        "minimum_size_bytes": "",
        "maximum_size_bytes": "",
        "excluded_tokens": "CAM, TS, TeleSync",
        "preferred_resolutions": [],
        "preferred_audio": [],
        "preferred_hdr": [],
        "preferred_size_band_min": [],
        "preferred_size_band_max": [],
    }
    values.update(overrides)
    return values


def test_quality_get_creates_movie_and_tv_profiles_and_renders_both_forms(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-default.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/quality")

    assert response.status_code == 200
    assert 'action="/quality/movie"' in response.text
    assert 'action="/quality/tv"' in response.text
    assert response.text.count('name="preferred_size_band_min"') == 32
    assert response.text.count('name="preferred_size_band_max"') == 32
    with Session(app.state.engine) as session:
        profiles = session.exec(select(QualityProfile).order_by(QualityProfile.media_type)).all()
    assert [(profile.media_type, profile.allowed_resolutions, profile.minimum_seeders) for profile in profiles] == [
        (MediaType.MOVIE, ["1080p", "2160p"], 5),
        (MediaType.TV, ["1080p", "2160p"], 5),
    ]


def test_quality_post_normalizes_and_updates_only_the_movie_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-update.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/quality/movie",
            data=_quality_form(
                allowed_resolutions=["1080p", "4K"],
                allowed_audio=["atmos"],
                minimum_seeders="12",
                excluded_tokens=" CAM, TeleSync ",
                preferred_resolutions=["4K", "1080p"],
                preferred_audio=["Atmos"],
                preferred_size_band_min=["1000000000", ""],
                preferred_size_band_max=["4000000000", ""],
            ),
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/quality"
    with Session(app.state.engine) as session:
        movie = _profile(session, MediaType.MOVIE)
        tv = _profile(session, MediaType.TV)
    assert movie.allowed_resolutions == ["1080p", "2160p"]
    assert movie.allowed_audio == ["atmos"]
    assert movie.allowed_hdr == []
    assert movie.minimum_seeders == 12
    assert movie.minimum_size_bytes is None
    assert movie.maximum_size_bytes is None
    assert movie.excluded_tokens == ["CAM", "TeleSync"]
    assert movie.preferred_resolutions == ["2160p", "1080p"]
    assert movie.preferred_audio == ["atmos"]
    assert movie.preferred_size_bands == [
        {"min_bytes": 1_000_000_000, "max_bytes": 4_000_000_000}
    ]
    assert tv.allowed_audio == []
    assert tv.minimum_seeders == 5


def test_quality_post_updates_only_the_tv_profile_and_allows_empty_hard_lists(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-tv-update.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/quality/tv",
            data=_quality_form(
                allowed_resolutions=[],
                allowed_audio=[],
                allowed_hdr=[],
                minimum_seeders="0",
                preferred_hdr=["HDR10+", "Dolby Vision"],
            ),
            follow_redirects=False,
        )

    assert response.status_code == 303
    with Session(app.state.engine) as session:
        movie = _profile(session, MediaType.MOVIE)
        tv = _profile(session, MediaType.TV)
    assert movie.allowed_resolutions == ["1080p", "2160p"]
    assert tv.allowed_resolutions == []
    assert tv.allowed_audio == []
    assert tv.allowed_hdr == []
    assert tv.minimum_seeders == 0
    assert tv.minimum_size_bytes is None
    assert tv.maximum_size_bytes is None
    assert tv.preferred_hdr == ["hdr10plus", "dolby_vision"]


def test_quality_template_preserves_submitted_tokens_and_scopes_error_to_submitted_form(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-submitted-values.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/quality/movie",
            data=_quality_form(excluded_tokens="CAM, cam"),
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert 'value="CAM, cam"' in response.text
    assert 'value="C, A, M, ,,  , c, a, m"' not in response.text
    assert response.text.count('data-error-field="excluded_tokens"') == 1


def test_quality_template_renders_persisted_zero_size_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-zero-limits.db"))
    app = create_app()

    with TestClient(app) as client:
        saved = client.post(
            "/quality/movie",
            data=_quality_form(minimum_size_bytes="0", maximum_size_bytes="0"),
            follow_redirects=False,
        )
        response = client.get("/quality")

    assert saved.status_code == 303
    movie_form = response.text.split('action="/quality/movie"', 1)[1].split("</form>", 1)[0]
    assert 'name="minimum_size_bytes" min="0" value="0"' in movie_form
    assert 'name="maximum_size_bytes" min="0" value="0"' in movie_form


@pytest.mark.parametrize(
    ("data", "error_field", "error_message"),
    [
        (_quality_form(minimum_seeders=None), "minimum_seeders", "Must be an integer"),
        (_quality_form(excluded_tokens=None), "excluded_tokens", "Values cannot be blank"),
    ],
)
def test_quality_post_omitted_required_field_renders_template_error(
    tmp_path, monkeypatch, data, error_field, error_message
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-omitted-field.db"))
    app = create_app()
    data = {key: value for key, value in data.items() if value is not None}

    with TestClient(app) as client:
        response = client.post("/quality/movie", data=data, follow_redirects=False)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert error_message in response.text
    assert f'data-error-field="{error_field}"' in response.text


@pytest.mark.parametrize(
    ("data", "error_field", "error_message"),
    [
        (_quality_form(allowed_resolutions=["invalid"]), "allowed_resolutions", "Unknown quality value"),
        (_quality_form(allowed_audio=["invalid"]), "allowed_audio", "Unknown quality value"),
        (_quality_form(allowed_hdr=["invalid"]), "allowed_hdr", "Unknown quality value"),
        (_quality_form(minimum_seeders="-1"), "minimum_seeders", "Must be an integer"),
        (_quality_form(minimum_size_bytes="invalid"), "minimum_size_bytes", "Must be an integer"),
        (_quality_form(maximum_size_bytes="invalid"), "maximum_size_bytes", "Must be an integer"),
        (_quality_form(excluded_tokens="CAM,,TS"), "excluded_tokens", "Values cannot be blank"),
        (_quality_form(preferred_resolutions=["invalid"]), "preferred_resolutions", "Unknown quality value"),
        (_quality_form(preferred_audio=["invalid"]), "preferred_audio", "Unknown quality value"),
        (_quality_form(preferred_hdr=["invalid"]), "preferred_hdr", "Unknown quality value"),
        (_quality_form(preferred_size_band_min=["1"], preferred_size_band_max=[]), "preferred_size_bands", "Each size band needs both a minimum and maximum"),
    ],
)
def test_quality_post_returns_field_error_without_partial_write(
    tmp_path, monkeypatch, data, error_field, error_message
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-invalid.db"))
    app = create_app()

    with TestClient(app) as client:
        client.get("/quality")
        with Session(app.state.engine) as session:
            movie_before = _profile(session, MediaType.MOVIE).model_dump()
            tv_before = _profile(session, MediaType.TV).model_dump()
        response = client.post("/quality/movie", data=data, follow_redirects=False)

    assert response.status_code == 422
    assert error_message in response.text
    assert f'data-error-field="{error_field}"' in response.text
    with Session(app.state.engine) as session:
        assert _profile(session, MediaType.MOVIE).model_dump() == movie_before
        assert _profile(session, MediaType.TV).model_dump() == tv_before


def test_quality_routes_require_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-auth.db"))
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/quality", follow_redirects=False)
        post_response = client.post("/quality/movie", data=_quality_form(), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/quality"
    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/login?next=/quality/movie"


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
