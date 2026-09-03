# Quality Profile and Automatic Grab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one editable global quality profile and opt-in, safe movie auto-grab for subscriptions.

**Architecture:** A singleton SQLModel profile is evaluated by a pure quality matcher. The subscription scanner persists every discovery, selects one eligible movie candidate, and calls a shared job-creation service used by both automatic and manual Grab flows.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, Jinja2, pytest.

---

## File structure

- Create `src/skald/quality.py` for profile parsing, release matching, and deterministic ranking.
- Create `src/skald/services/grab.py` for shared, validated qBittorrent/job creation.
- Create `src/skald/routes/quality.py` and `src/skald/templates/quality.html`.
- Modify `models.py`, `db.py`, `subscriptions.py`, `routes/jobs.py`, `routes/subscriptions.py`, `main.py`, base template/style, and tests.

### Task 1: Persist and validate the global profile

**Files:**
- Modify: `src/skald/models.py`, `src/skald/db.py`
- Create: `src/skald/quality.py`, `tests/test_quality.py`

- [ ] **Step 1: Write failing defaults and matcher tests**

```python
def test_default_profile_accepts_1080p_with_five_seeders():
    profile = default_quality_profile()
    release = ReleaseResult("Movie.2026.1080p.WEB", "fake", 1, 5, 0, "magnet:?one")
    assert profile_matches(profile, release)


def test_profile_rejects_cam_ts_and_telesync_case_insensitively():
    profile = default_quality_profile()
    for title in ("Movie.1080p.CAM", "Movie.4K.TS", "Movie.1080p.TeleSync"):
        assert not profile_matches(profile, ReleaseResult(title, "fake", 1, 99, 0, "magnet:?x"))
```

- [ ] **Step 2: Run RED tests**

Run: `uv run pytest tests/test_quality.py -q`  
Expected: FAIL because profile functions do not exist.

- [ ] **Step 3: Implement models and pure matcher**

Add a singleton `QualityProfile` with ID 1, JSON `allowed_resolutions` and
`excluded_tokens`, `minimum_seeders`, and `updated_at`. Add
`auto_download: bool = False` and nullable `auto_grabbed_release_id` to
`MediaSubscription`; extend `migrate_schema()` with additive columns only.

Implement `default_quality_profile()`, `profile_matches(profile, release)`,
and `best_matching_release(profile, releases)`. Normalize `4K`/`2160p`, accept
only 1080p/2160p defaults, require five seeders, reject excluded tokens at word
boundaries, and rank by seeders, resolution, then title.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `uv run pytest tests/test_quality.py -q`  
Expected: PASS.

Commit: `git add src/skald/models.py src/skald/db.py src/skald/quality.py tests/test_quality.py && git commit -m "feat: add quality profile"`.

### Task 2: Extract the safe shared Grab service

**Files:**
- Create: `src/skald/services/grab.py`
- Modify: `src/skald/routes/jobs.py`
- Create: `tests/test_grab_service.py`

- [ ] **Step 1: Write a failing shared-service test**

```python
def test_create_movie_job_adds_torrent_then_persists_job(session, qbit):
    job = create_media_job(session, qbit, release, media_type=MediaType.MOVIE,
                           title="Movie", year=2026, settings=settings)
    assert qbit.add_calls == [(release.download_url, settings.category_movie)]
    assert job.qbit_hash == "fakehash"
```

- [ ] **Step 2: Run RED test**

Run: `uv run pytest tests/test_grab_service.py -q`  
Expected: FAIL because `create_media_job` does not exist.

- [ ] **Step 3: Implement and reuse the service**

Move the existing validated category selection, qBittorrent add, and
`MediaJob` persistence from `routes/jobs.py` into
`create_media_job(session, qbit, release, *, media_type, title, year, season,
episode, episode_set, settings)`. Keep all existing movie/TV metadata
validation and HTTP error mapping in the route; the service raises a typed
creation error on qBittorrent failure. Replace the route’s duplicated body with
one service call.

- [ ] **Step 4: Verify compatibility and commit**

Run: `uv run pytest tests/test_grab_service.py tests/test_routes.py -q`  
Expected: PASS.

Commit: `git add src/skald/services/grab.py src/skald/routes/jobs.py tests/test_grab_service.py tests/test_routes.py && git commit -m "refactor: share grab creation service"`.

### Task 3: Select one automatic movie release

**Files:**
- Modify: `src/skald/subscriptions.py`, `tests/test_subscriptions.py`

- [ ] **Step 1: Write failing scanner tests**

```python
async def test_auto_download_uses_highest_seed_matching_movie(session, subscription, indexer, qbit):
    subscription.auto_download = True
    await scan_due_subscriptions(session, indexer, qbit=qbit, settings=settings, now=now)
    assert qbit.add_calls == [("magnet:?best", "skald-movie")]
    assert subscription.auto_grabbed_release_id is not None


async def test_auto_download_never_grabs_tv_without_episode_scope(session, tv_subscription, indexer, qbit):
    tv_subscription.auto_download = True
    await scan_due_subscriptions(session, indexer, qbit=qbit, settings=settings, now=now)
    assert qbit.add_calls == []
```

- [ ] **Step 2: Run RED tests**

Run: `uv run pytest tests/test_subscriptions.py -q`  
Expected: FAIL because scanner has no auto-grab dependency or selection.

- [ ] **Step 3: Implement auto-grab after discovery persistence**

Pass `qbit`, settings, and a profile provider into the scanner from the worker.
After inserting all new release notifications, select only newly discovered
movie releases that match the singleton profile. If `auto_download` is true and
`auto_grabbed_release_id` is null, pass the highest ranked release to the shared
Grab service. On success persist the selected release ID; on failure save a
bounded error and leave it null. Never call the service for TV subscriptions.

- [ ] **Step 4: Add safety tests and commit**

Cover disabled auto-download, no matching release, qBittorrent failure retry,
and repeat scans creating no second job. Run:
`uv run pytest tests/test_subscriptions.py tests/test_worker.py -q`.

Commit: `git add src/skald/subscriptions.py src/skald/worker.py src/skald/main.py tests/test_subscriptions.py tests/test_worker.py && git commit -m "feat: auto grab matching movies"`.

### Task 4: Add Quality settings and subscription controls

**Files:**
- Create: `src/skald/routes/quality.py`, `src/skald/templates/quality.html`
- Modify: `src/skald/routes/subscriptions.py`, `src/skald/templates/subscriptions.html`, `src/skald/templates/base.html`, `src/skald/static/style.css`, `src/skald/main.py`
- Modify: `tests/test_subscriptions.py`; create `tests/test_quality_routes.py`

- [ ] **Step 1: Write failing route tests**

```python
def test_quality_page_creates_default_profile(client):
    response = client.get("/quality")
    assert response.status_code == 200
    assert "1080p" in response.text
    assert "TeleSync" in response.text


def test_subscription_auto_download_toggle(client, subscription):
    response = client.post(f"/subscriptions/{subscription.id}/auto-download", follow_redirects=False)
    assert response.status_code == 303
```

- [ ] **Step 2: Run RED tests**

Run: `uv run pytest tests/test_quality_routes.py tests/test_subscriptions.py -q`  
Expected: FAIL with 404 routes.

- [ ] **Step 3: Implement protected settings and UI**

Implement `GET/POST /quality` with validation for one or more allowed
resolutions, non-negative seeders, and comma-separated unique non-empty tokens.
Register it with existing authentication. Add a `Quality` nav link. On each
subscription row render an auto-download status and a POST toggle; TV rows state
that episode scope is required before automatic download can run. The designer
owns layout, spacing, and responsive CSS while preserving existing dark patterns.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/test_quality_routes.py tests/test_subscriptions.py tests/test_routes.py -q`  
Expected: PASS.

Commit: `git add src/skald/routes/quality.py src/skald/templates/quality.html src/skald/routes/subscriptions.py src/skald/templates/subscriptions.html src/skald/templates/base.html src/skald/static/style.css src/skald/main.py tests/test_quality_routes.py tests/test_subscriptions.py && git commit -m "feat: configure automatic subscription downloads"`.

### Task 5: Regression verification and documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document global profile defaults and TV safety block**

State the global defaults, per-subscription opt-in, one-release selection, and
that TV auto-download waits for season/episode scope.

- [ ] **Step 2: Run full verification**

Run: `uv run python -m compileall src && uv run pytest -q && git diff --check`  
Expected: all commands exit 0.

- [ ] **Step 3: Commit documentation**

Commit: `git add README.md && git commit -m "docs: explain quality profiles"`.
