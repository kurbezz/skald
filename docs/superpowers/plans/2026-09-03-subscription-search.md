# Subscription Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add TMDB-backed movie and TV subscriptions that periodically discover Torznab releases and show them as in-app notifications without downloading anything.

**Architecture:** An HTTPX TMDB client normalizes catalog records. SQLModel subscription and discovery tables store durable schedules and idempotent release fingerprints; a focused scanner runs from the existing short worker loop only for due subscriptions. An authenticated server-rendered router owns catalog search and subscription actions.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, HTTPX, Jinja2, pytest, respx.

---

## File structure

- Create `src/skald/tmdb.py`: TMDB client, normalized media record, safe error type.
- Create `src/skald/subscriptions.py`: query construction, fingerprints, and due-scan service.
- Create `src/skald/routes/subscriptions.py`: protected catalog/list/action routes.
- Create `src/skald/templates/subscriptions.html`: server-rendered page.
- Create `tests/test_tmdb.py` and `tests/test_subscriptions.py`.
- Modify `src/skald/models.py`, `config.py`, `main.py`, `worker.py`, `templates/base.html`, `static/style.css`, `tests/test_config.py`, and `README.md`.

`src/skald/db.py` remains unchanged: `SQLModel.metadata.create_all(engine)` already runs before migrations and creates these additive tables for existing SQLite databases.

### Task 1: Add settings and persistent models

**Files:**
- Modify: `src/skald/config.py:6-33`
- Modify: `src/skald/models.py:37-79`
- Modify: `tests/test_config.py:4-22`
- Create: `tests/test_subscriptions.py`

- [ ] **Step 1: Write the failing settings test**

```python
def test_settings_reads_subscription_values(monkeypatch):
    monkeypatch.setenv("TMDB_READ_ACCESS_TOKEN", "tmdb-token")
    monkeypatch.setenv("SUBSCRIPTION_CHECK_INTERVAL_SECONDS", "21600")
    settings = Settings(_env_file=None)
    assert settings.tmdb_read_access_token == "tmdb-token"
    assert settings.subscription_check_interval_seconds == 21_600
```

- [ ] **Step 2: Run the test to confirm failure**

Run: `pytest tests/test_config.py::test_settings_reads_subscription_values -q`  
Expected: FAIL because both settings are absent.

- [ ] **Step 3: Add settings and default assertions**

Append to `Settings`:

```python
    tmdb_read_access_token: str = ""
    subscription_check_interval_seconds: int = 6 * 60 * 60
```

Add both environment names to `test_settings_defaults`' clear list and assert
the empty token plus `21_600` seconds. Run: `pytest tests/test_config.py -q`.
Expected: PASS.

- [ ] **Step 4: Write failing uniqueness tests**

```python
def test_subscription_and_release_fingerprints_are_unique(session):
    subscription = MediaSubscription(tmdb_id=603, type=MediaType.MOVIE, title="The Matrix")
    session.add(subscription)
    session.commit()
    session.add_all([
        SubscriptionRelease(subscription_id=subscription.id, release_title="The.Matrix.1999", indexer="fake", size_bytes=1, seeders=1, leechers=0, download_url="magnet:?one", fingerprint="same"),
        SubscriptionRelease(subscription_id=subscription.id, release_title="The.Matrix.1999", indexer="fake", size_bytes=1, seeders=1, leechers=0, download_url="magnet:?one", fingerprint="same"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()
```

Use a temporary database `session` fixture that enters `app.router.lifespan_context(app)`. Add a parallel test inserting two `MediaSubscription` objects with TMDB ID `603` and `MediaType.MOVIE`; it must raise `IntegrityError` on commit.

- [ ] **Step 5: Implement the records**

Import `UniqueConstraint` from `sqlalchemy` and append these models to
`src/skald/models.py`, reusing `_utcnow` and `MediaType`:

```python
class MediaSubscription(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("tmdb_id", "type", name="uq_subscription_tmdb_type"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    tmdb_id: int = Field(index=True)
    type: MediaType
    title: str
    original_title: Optional[str] = None
    year: Optional[int] = None
    poster_url: Optional[str] = None
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)
    last_checked_at: Optional[datetime] = None
    next_check_at: datetime = Field(default_factory=_utcnow, index=True)
    last_error: Optional[str] = None


class SubscriptionRelease(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="mediasubscription.id", index=True)
    release_title: str
    indexer: str
    size_bytes: int
    seeders: int
    leechers: int
    download_url: str
    published_at: Optional[datetime] = None
    discovered_at: datetime = Field(default_factory=_utcnow, index=True)
    fingerprint: str = Field(index=True, sa_column_kwargs={"unique": True})
    read_at: Optional[datetime] = None
```

- [ ] **Step 6: Validate and commit**

Run: `pytest tests/test_config.py tests/test_subscriptions.py -q`  
Expected: PASS.

Commit: `git add src/skald/config.py src/skald/models.py tests/test_config.py tests/test_subscriptions.py && git commit -m "feat: add subscription persistence"`.

### Task 2: Build and test the TMDB client

**Files:**
- Create: `src/skald/tmdb.py`
- Create: `tests/test_tmdb.py`

- [ ] **Step 1: Write failing normalized-search tests**

```python
@respx.mock
async def test_search_normalizes_movie_and_tv():
    route = respx.get("https://api.themoviedb.org/3/search/multi").mock(
        return_value=Response(200, json={"results": [
            {"id": 603, "media_type": "movie", "title": "The Matrix", "original_title": "The Matrix", "release_date": "1999-03-30", "poster_path": "/matrix.jpg"},
            {"id": 1396, "media_type": "tv", "name": "Breaking Bad", "original_name": "Breaking Bad", "first_air_date": "2008-01-20", "poster_path": None},
            {"id": 1, "media_type": "person", "name": "Keanu Reeves"},
        ]})
    )
    results = await TmdbClient("token").search("matrix")
    assert route.called
    assert [(item.tmdb_id, item.type, item.year, item.poster_url) for item in results] == [
        (603, MediaType.MOVIE, 1999, "https://image.tmdb.org/t/p/w342/matrix.jpg"),
        (1396, MediaType.TV, 2008, None),
    ]


@respx.mock
async def test_tmdb_error_hides_token():
    respx.get("https://api.themoviedb.org/3/search/multi").mock(return_value=Response(401))
    with pytest.raises(TmdbError, match="TMDB request failed") as exc_info:
        await TmdbClient("secret-token").search("matrix")
    assert "secret-token" not in str(exc_info.value)
```

- [ ] **Step 2: Confirm failure**

Run: `pytest tests/test_tmdb.py -q`  
Expected: FAIL with `ModuleNotFoundError: No module named 'skald.tmdb'`.

- [ ] **Step 3: Implement the client contract**

```python
@dataclass(frozen=True)
class TmdbMedia:
    tmdb_id: int
    type: MediaType
    title: str
    original_title: str | None
    year: int | None
    poster_url: str | None


class TmdbClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.token = token
        self.client = client or httpx.AsyncClient(base_url="https://api.themoviedb.org/3", timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self.token)
```

Implement `search(query)` as `GET /search/multi` with the Bearer token,
`query`, `language=en-US`, and `include_adult=false`. Implement
`get_media(tmdb_id, media_type)` as `GET /movie/{id}` or `/tv/{id}`. Both use
one normalizer that rejects people/missing IDs/missing titles and extracts the
first four digits of the appropriate date. Lazily request `/configuration` once
per client, cache `images.secure_base_url`, confirm that `w342` is supported,
and build `{secure_base_url}w342{poster_path}` only when a path exists. Convert
every `httpx.HTTPError` to `TmdbError("TMDB request failed")`; when unconfigured
raise `TmdbError("TMDB is not configured")` before HTTP work. Add
`async def aclose(self) -> None` to close the owned HTTPX client.

- [ ] **Step 4: Add edge coverage and verify**

The test module must cover an empty date and absent poster (yielding `None`),
an unsupported media type (omitted), `get_media(603, MediaType.MOVIE)`, one
cached `/configuration` request across two poster results, HTTP 429, and
`httpx.ConnectError`. Each failure asserts the public exception text and that it
excludes the token. Run: `pytest tests/test_tmdb.py -q`.
Expected: PASS.

- [ ] **Step 5: Commit the client**

Commit: `git add src/skald/tmdb.py tests/test_tmdb.py && git commit -m "feat: add tmdb catalog client"`.

### Task 3: Scan due subscriptions without automatic downloads

**Files:**
- Create: `src/skald/subscriptions.py`
- Modify: `src/skald/worker.py:1-19,58-105,912-928`
- Modify: `src/skald/main.py:12-17,29-43`
- Modify: `tests/test_subscriptions.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: Write a failing due-scan test**

```python
async def test_due_scan_records_first_result_then_deduplicates(session, subscription):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    indexer = ScanIndexer([ReleaseResult("The.Matrix.1999.1080p", "fake", 1, 2, 3, "magnet:?one")])
    await scan_due_subscriptions(session, indexer, interval_seconds=21_600, now=now)
    subscription.next_check_at = now + timedelta(hours=6)
    session.add(subscription)
    session.commit()
    await scan_due_subscriptions(session, indexer, interval_seconds=21_600, now=now + timedelta(hours=6))
    assert len(session.exec(select(SubscriptionRelease)).all()) == 1
    assert indexer.queries == ["The Matrix 1999", "The Matrix 1999"]
    assert session.exec(select(MediaJob)).all() == []
```

`ScanIndexer.search` appends the query and returns its supplied releases. The
fixture creates a due Matrix movie subscription. The test module must also
prove that inactive and future subscriptions are not queried, a TV result is
filtered from a movie subscription, and one indexer failure does not block the
next due item.

- [ ] **Step 2: Confirm failure**

Run: `pytest tests/test_subscriptions.py -q`  
Expected: FAIL because `scan_due_subscriptions` is absent.

- [ ] **Step 3: Implement the scanner**

```python
def release_fingerprint(subscription_id: int, release: ReleaseResult) -> str:
    value = "\x1f".join((str(subscription_id), release.indexer, release.download_url, release.title, str(release.size_bytes)))
    return hashlib.sha256(value.encode()).hexdigest()


def subscription_query(subscription: MediaSubscription) -> str:
    title = subscription.original_title or subscription.title
    return f"{title} {subscription.year}" if subscription.year else title
```

Implement `scan_due_subscriptions(session, indexer, *, interval_seconds, now)`
to select active subscriptions whose `next_check_at <= now`. For each item:
search the existing indexer, parse each title, retain only the matching
`MediaType`, skip an existing fingerprint, and insert a `SubscriptionRelease`.
On success set `last_checked_at`, clear `last_error`, and set `next_check_at` to
`now + timedelta(seconds=interval_seconds)`. On one failure roll back, reload
only that subscription, save `last_error` and the next check, then continue.
Never import qBittorrent, job routes, or `MediaJob`.

- [ ] **Step 4: Wire the scanner into the worker**

Add `indexer` and `subscription_check_interval_seconds` arguments to
`poll_once` and `worker_loop`; call the scanner once per `poll_once`. In
`main.py`, pass `app.state.indexer` and the new setting. Preserve the existing
`asyncio.sleep(poll_interval_seconds)`; six hours is a durable per-subscription
schedule, not a new worker sleep interval.

- [ ] **Step 5: Validate and commit**

Run: `pytest tests/test_subscriptions.py tests/test_worker.py -q`  
Expected: PASS.

Commit: `git add src/skald/subscriptions.py src/skald/worker.py src/skald/main.py tests/test_subscriptions.py tests/test_worker.py && git commit -m "feat: scan media subscriptions"`.

### Task 4: Build the protected subscriptions interface

**Files:**
- Create: `src/skald/routes/subscriptions.py`
- Create: `src/skald/templates/subscriptions.html`
- Modify: `src/skald/main.py:14-16,50-52`
- Modify: `src/skald/templates/base.html:16-25`
- Modify: `src/skald/static/style.css`
- Modify: `tests/test_subscriptions.py`

- [ ] **Step 1: Write failing route tests**

```python
def test_catalog_search_and_create_subscription(client, app):
    app.state.tmdb = FakeTmdb([TmdbMedia(603, MediaType.MOVIE, "The Matrix", "The Matrix", 1999, None)])
    response = client.get("/subscriptions", params={"q": "matrix"})
    created = client.post("/subscriptions", data={"tmdb_id": 603, "media_type": "movie"}, follow_redirects=False)
    assert response.status_code == 200
    assert "The Matrix" in response.text
    assert created.status_code == 303
    assert created.headers["location"] == "/subscriptions"


def test_subscription_actions_toggle_read_and_delete(client, session, subscription, release):
    assert client.post(f"/subscriptions/{subscription.id}/toggle", follow_redirects=False).status_code == 303
    assert client.post(f"/subscriptions/{subscription.id}/releases/read", follow_redirects=False).status_code == 303
    assert client.post(f"/subscriptions/{subscription.id}/delete", follow_redirects=False).status_code == 303
    assert session.get(MediaSubscription, subscription.id) is None
    assert session.exec(select(SubscriptionRelease)).all() == []
```

`FakeTmdb.search` and `FakeTmdb.get_media` are async and return `TmdbMedia`.
The module must test a missing token, `TmdbError`, duplicate creation, a 404
action ID, and the existing configured-auth redirect behavior.

- [ ] **Step 2: Confirm failure**

Run: `pytest tests/test_subscriptions.py -q`  
Expected: FAIL with 404 responses before router registration.

- [ ] **Step 3: Implement routes and app wiring**

Create these concrete router endpoints: `GET /subscriptions?q=`,
`POST /subscriptions`, `POST /subscriptions/{subscription_id}/toggle`,
`POST /subscriptions/{subscription_id}/releases/read`, and
`POST /subscriptions/{subscription_id}/delete`. The create endpoint accepts
only `tmdb_id: int = Form()` and `media_type: MediaType = Form()`.

The create route must call `app.state.tmdb.get_media(tmdb_id, media_type)` and
never trust browser-provided titles. Duplicate creation redirects without a
second row. Toggle changes `is_active`, setting `next_check_at=_utcnow()` on
enable. Mark-read sets `read_at=_utcnow()` on unread rows. Delete explicitly
removes the child rows before the subscription. Missing records raise 404.
Initialize `app.state.tmdb = TmdbClient(settings.tmdb_read_access_token)` in
lifespan, call `await app.state.tmdb.aclose()` during lifespan shutdown after
cancelling the worker task, and register the router with `Depends(require_auth)`.

- [ ] **Step 4: Implement the template and navigation**

Extend `base.html`. Render: TMDB attribution; missing-token/search-error
alerts; GET `q` search; catalog result forms containing only `tmdb_id` and
`media_type`; subscription rows with state, last check/error, unread count, and
toggle/delete/read forms; and newest-first release rows with indexer, size,
peers, timestamp, and a `/search?q=` link. Add the `Subscriptions` nav link.
Reuse existing panel/button/badge/table/alert styles, adding
only narrow `subscription-*` selectors for poster width, actions, and the
existing mobile breakpoint. The page must function without JavaScript.

- [ ] **Step 5: Validate and commit**

Run: `pytest tests/test_subscriptions.py tests/test_routes.py -q`  
Expected: PASS and no test creates a qBittorrent call.

Commit: `git add src/skald/routes/subscriptions.py src/skald/templates/subscriptions.html src/skald/templates/base.html src/skald/static/style.css src/skald/main.py tests/test_subscriptions.py && git commit -m "feat: add subscriptions interface"`.

### Task 5: Document and verify the completed feature

**Files:**
- Modify: `README.md:1-88`
- Modify: `docs/superpowers/specs/2026-09-03-subscription-search-design.md:1-188`

- [ ] **Step 1: Document setup and behavior**

Document `TMDB_READ_ACCESS_TOKEN`, TMDB attribution, the six-hour default, and
that discoveries are in-app only: no automatic torrent or `MediaJob` is made.

- [ ] **Step 2: Run complete verification**

Run: `python -m compileall src && pytest -q && git diff --check`  
Expected: exit status 0.

- [ ] **Step 3: Verify the safety invariant**

With a temporary database, create the same subscription twice and run two due
scans returning one identical `ReleaseResult`. Assert one subscription, one
`SubscriptionRelease`, and zero `MediaJob` rows. Disable the subscription, make
it due, rerun the scanner, and assert the fake indexer receives no new query.

- [ ] **Step 4: Commit documentation**

Commit: `git add README.md docs/superpowers/specs/2026-09-03-subscription-search-design.md && git commit -m "docs: document subscription search"`.
