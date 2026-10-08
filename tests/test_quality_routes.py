from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from skald.auth import create_csrf_token
from skald.main import create_app
from skald.models import MediaSubscription, MediaType, QualityProfile


def _profile(session, media_type):
    return session.exec(
        select(QualityProfile).where(QualityProfile.media_type == media_type)
    ).one()


def _quality_form(**overrides):
    values = {
        "csrf_token": create_csrf_token(None),
        "allowed_resolutions": ["1080p"],
        "allowed_audio": [],
        "allowed_hdr": [],
        "minimum_seeders": "5",
        "minimum_size_gib": "",
        "maximum_size_gib": "",
        "excluded_tokens": "CAM, TS, TeleSync",
        "preferred_resolutions": [],
        "preferred_audio": [],
        "preferred_hdr": [],
        "preferred_size_band_min_gib": [],
        "preferred_size_band_max_gib": [],
    }
    values.update(overrides)
    return values


def test_quality_get_creates_movie_and_tv_profiles_and_renders_one_form(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-default.db"))
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/quality")

    assert response.status_code == 200
    assert 'action="/quality/movie"' in response.text
    assert 'action="/quality/tv"' not in response.text
    assert 'aria-current="page"' in response.text
    assert response.text.count('name="preferred_size_band_min_gib"') == 1
    with Session(app.state.engine) as session:
        profiles = session.exec(select(QualityProfile).order_by(QualityProfile.media_type)).all()
    assert [(profile.media_type, profile.allowed_resolutions, profile.minimum_seeders) for profile in profiles] == [
        (MediaType.MOVIE, ["1080p", "2160p"], 5),
        (MediaType.TV, ["1080p", "2160p"], 5),
    ]


def test_quality_post_rejects_missing_csrf_token_and_get_renders_usable_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-csrf.db"))
    app = create_app()

    with TestClient(app) as client:
        page = client.get("/quality")
        form = _quality_form()
        form.pop("csrf_token")
        rejected = client.post("/quality/movie", data=form, follow_redirects=False)
        validation_error = client.post(
            "/quality/movie",
            data=_quality_form(excluded_tokens="CAM, cam"),
            follow_redirects=False,
        )

    assert page.status_code == 200
    assert 'type="hidden" name="csrf_token" value="' in page.text
    assert rejected.status_code == 403
    assert validation_error.status_code == 422
    assert f'name="csrf_token" value="{create_csrf_token(None)}"' in validation_error.text


@pytest.mark.parametrize(
    "csrf_tokens",
    [
        ("invalid-token", create_csrf_token(None)),
        (create_csrf_token(None), "invalid-token"),
    ],
)
def test_quality_post_rejects_duplicate_csrf_tokens_without_mutating_profile(
    tmp_path, monkeypatch, csrf_tokens
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-duplicate-csrf.db"))
    app = create_app()

    with TestClient(app) as client:
        client.get("/quality")
        with Session(app.state.engine) as session:
            movie_before = _profile(session, MediaType.MOVIE).model_dump()
        response = client.post(
            "/quality/movie",
            content=urlencode(_quality_form(csrf_token=csrf_tokens), doseq=True),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    assert response.status_code == 403
    with Session(app.state.engine) as session:
        assert _profile(session, MediaType.MOVIE).model_dump() == movie_before


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
                preferred_size_band_min_gib=["1", ""],
                preferred_size_band_max_gib=["2.5", ""],
            ),
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/quality?profile=movie"
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
        {"min_bytes": 2**30, "max_bytes": int(2.5 * 2**30)}
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
            data=_quality_form(minimum_size_gib="0", maximum_size_gib="0"),
            follow_redirects=False,
        )
        response = client.get("/quality")

    assert saved.status_code == 303
    assert 'name="minimum_size_gib" value="0"' in response.text
    assert 'name="maximum_size_gib" value="0"' in response.text


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
        (_quality_form(minimum_size_gib="invalid"), "minimum_size_bytes", "Enter a size in GiB"),
        (_quality_form(maximum_size_gib="invalid"), "maximum_size_bytes", "Enter a size in GiB"),
        (_quality_form(excluded_tokens="CAM,,TS"), "excluded_tokens", "Values cannot be blank"),
        (_quality_form(preferred_resolutions=["invalid"]), "preferred_resolutions", "Unknown quality value"),
        (_quality_form(preferred_audio=["invalid"]), "preferred_audio", "Unknown quality value"),
        (_quality_form(preferred_hdr=["invalid"]), "preferred_hdr", "Unknown quality value"),
        (_quality_form(preferred_size_band_min_gib=["1"], preferred_size_band_max_gib=[]), "preferred_size_bands", "Each size band needs both a minimum and maximum"),
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
    assert post_response.headers["location"] == "/login?expired=1&next=/jobs"


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
            f"/subscriptions/{subscription_id}/auto-download",
            data={"csrf_token": create_csrf_token(None)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/subscriptions"
    with Session(app.state.engine) as session:
        assert session.get(MediaSubscription, subscription_id).auto_download is True


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_form = False
        self.fields: list[tuple[str, str]] = []
        self._select = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and "/quality/" in (a.get("action") or ""):
            self.in_form = True
            self.action = a["action"]
        elif not self.in_form:
            return
        elif tag == "input":
            kind = a.get("type", "text")
            if not a.get("name"):
                return
            if kind in {"checkbox", "radio"} and "checked" not in a:
                return
            self.fields.append((a["name"], a.get("value") or ""))
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select and "selected" in a:
            self.fields.append((self._select, a.get("value") or ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False
        if tag == "select":
            self._select = None


class _Fields(list):
    def as_data(self):
        data = {}
        for key, value in self:
            data.setdefault(key, []).append(value)
        return data


def _parse_form(html):
    parser = _FormParser()
    parser.feed(html)
    return parser.action, _Fields(parser.fields)


@pytest.mark.parametrize("profile", ["movie", "tv"])
def test_quality_form_round_trips_unchanged(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-roundtrip.db"))
    app = create_app()

    with TestClient(app) as client:
        page = client.get(f"/quality?profile={profile}")
        action, fields = _parse_form(page.text)
        assert action == f"/quality/{profile}"
        assert ("preferred_hdr", "") in fields
        response = client.post(action, data=fields.as_data(), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/quality?profile={profile}"


def test_quality_every_option_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-options.db"))
    app = create_app()
    options = {
        "allowed_resolutions": ["480p", "720p", "1080p", "2160p"],
        "allowed_audio": ["stereo", "5.1", "7.1", "atmos"],
        "allowed_hdr": ["sdr", "hdr", "hdr10", "hdr10plus", "dolby_vision"],
    }
    with TestClient(app) as client:
        for field, values in options.items():
            for value in values:
                data = _quality_form(**{"allowed_resolutions": [], field: [value]})
                first = client.post("/quality/movie", data=data, follow_redirects=False)
                assert first.status_code == 303, (field, value)
                page = client.get("/quality?profile=movie")
                _, fields = _parse_form(page.text)
                assert (field, value) in fields
                again = client.post("/quality/movie", data=fields.as_data(), follow_redirects=False)
                assert again.status_code == 303, (field, value)
                with Session(app.state.engine) as session:
                    assert getattr(_profile(session, MediaType.MOVIE), field) == [value]


def test_quality_size_gib_round_trip_and_bands(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-gib.db"))
    app = create_app()
    with TestClient(app) as client:
        saved = client.post(
            "/quality/tv",
            data=_quality_form(
                minimum_size_gib="0.5",
                maximum_size_gib="10",
                preferred_size_band_min_gib=["1", "3", ""],
                preferred_size_band_max_gib=["2", "4", ""],
            ),
            follow_redirects=False,
        )
        page = client.get("/quality?profile=tv")
        extra = client.get("/quality?profile=tv&extra_bands=2")
    assert saved.status_code == 303
    with Session(app.state.engine) as session:
        tv = _profile(session, MediaType.TV)
    assert tv.minimum_size_bytes == 2**29
    assert tv.maximum_size_bytes == 10 * 2**30
    assert tv.preferred_size_bands == [
        {"min_bytes": 2**30, "max_bytes": 2 * 2**30},
        {"min_bytes": 3 * 2**30, "max_bytes": 4 * 2**30},
    ]
    assert 'role="status"' in page.text
    assert "TV profile saved." in page.text
    assert page.text.count('name="preferred_size_band_min_gib"') == 3
    assert extra.text.count('name="preferred_size_band_min_gib"') == 5
    assert 'value="0.5"' in page.text


def test_quality_error_summary_and_selected_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality-errors.db"))
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/quality/tv",
            data=_quality_form(minimum_seeders="abc", excluded_tokens="X, Y"),
            follow_redirects=False,
        )
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert 'href="#f-minimum-seeders"' in response.text
    assert 'aria-invalid="true" aria-describedby="err-minimum_seeders"' in response.text
    assert 'value="abc"' in response.text
    assert 'value="X, Y"' in response.text
    assert 'action="/quality/tv"' in response.text
    assert '<a href="/quality?profile=tv" aria-current="page"' in response.text
