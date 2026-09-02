# Search Peer Sorting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sort search results by seeders or leechers, defaulting to the most seeders first.

**Architecture:** The `/search` route validates `sort` and `direction` query parameters, then orders `ReleaseResult` objects before enriching and rendering them. The result-table headers link to server-rendered sorted views and preserve the search query and media type; CSS makes those links identifiable and exposes their active state.

**Tech Stack:** Python 3, FastAPI, Jinja2 templates, pytest, CSS.

---

## File structure

- `src/skald/routes/search.py` — validates sort parameters, orders indexer results, and passes resolved sort state to the template.
- `src/skald/templates/search.html` — replaces the combined peer header with sortable Seeders and Leechers headers.
- `src/skald/static/style.css` — styles sort links, hover state, active state, and direction indicator without changing table layout behavior.
- `tests/test_routes.py` — verifies defaults, explicit sort modes, invalid-parameter fallback, and generated header links.

### Task 1: Route sorting tests

**Files:**
- Modify: `tests/test_routes.py:17-29`
- Modify: `tests/test_routes.py:53-96`

- [ ] **Step 1: Add a fake indexer with distinguishable peer counts**

Add below `FakeIndexer`:

```python
class MultiResultIndexer:
    async def search(self, query: str) -> list[ReleaseResult]:
        return [
            ReleaseResult(
                title="Seeder.High.2024.1080p",
                indexer="fake",
                size_bytes=1_000_000_000,
                seeders=20,
                leechers=2,
                download_url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
            ),
            ReleaseResult(
                title="Leecher.High.2024.1080p",
                indexer="fake",
                size_bytes=1_000_000_000,
                seeders=5,
                leechers=30,
                download_url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222",
            ),
            ReleaseResult(
                title="Middle.Peers.2024.1080p",
                indexer="fake",
                size_bytes=1_000_000_000,
                seeders=10,
                leechers=10,
                download_url="magnet:?xt=urn:btih:3333333333333333333333333333333333333333",
            ),
        ]
```

- [ ] **Step 2: Add failing route tests for sort modes and header links**

Add this test below `test_search_grab_and_jobs_pages`:

```python
@pytest.mark.parametrize(
    ("params", "expected_titles"),
    [
        ({}, ["Seeder.High", "Middle.Peers", "Leecher.High"]),
        ({"sort": "seeders", "direction": "asc"}, ["Leecher.High", "Middle.Peers", "Seeder.High"]),
        ({"sort": "leechers", "direction": "desc"}, ["Leecher.High", "Middle.Peers", "Seeder.High"]),
        ({"sort": "leechers", "direction": "asc"}, ["Seeder.High", "Middle.Peers", "Leecher.High"]),
        ({"sort": "unknown", "direction": "up"}, ["Seeder.High", "Middle.Peers", "Leecher.High"]),
    ],
)
def test_search_sorts_peers(tmp_path, monkeypatch, params, expected_titles):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search_sort.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = MultiResultIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie", **params})

    assert response.status_code == 200
    positions = [response.text.index(title) for title in expected_titles]
    assert positions == sorted(positions)
```

Then add this independent header-link test:

```python
def test_search_renders_sort_links(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search_sort_links.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = MultiResultIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie"})

    assert response.status_code == 200
    assert 'href="/search?q=matrix&amp;type=movie&amp;sort=seeders&amp;direction=asc"' in response.text
    assert 'href="/search?q=matrix&amp;type=movie&amp;sort=leechers&amp;direction=desc"' in response.text
```

- [ ] **Step 3: Run the new test to verify it fails**

Run: `uv run pytest tests/test_routes.py -k "search_sorts_peers or search_renders_sort_links" -v`

Expected: FAIL because the route does not yet accept, validate, or apply sort state and the template has no sort links.

### Task 2: Validate and apply server-side sorting

**Files:**
- Modify: `src/skald/routes/search.py:13-31`
- Test: `tests/test_routes.py:17-29, after test_search_grab_and_jobs_pages`

- [ ] **Step 1: Define supported sort values**

Add module-level constants above `search()`:

```python
SORT_FIELDS = {"seeders", "leechers"}
SORT_DIRECTIONS = {"asc", "desc"}
```

- [ ] **Step 2: Extend `search()` and resolve safe sort state**

Change its signature and beginning to:

```python
@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    type: str = "movie",
    sort: str = "seeders",
    direction: str = "desc",
):
    sort = sort if sort in SORT_FIELDS else "seeders"
    direction = direction if direction in SORT_DIRECTIONS else "desc"
    results = []
    error = None
```

- [ ] **Step 3: Sort releases before enriching them**

In the successful indexer branch, immediately after `releases = await indexer.search(q)` has completed, replace the existing loop with:

```python
            releases = sorted(
                releases,
                key=lambda release: getattr(release, sort),
                reverse=direction == "desc",
            )
            for release in releases:
                guess = parse_release(release.title)
                results.append({"release": release, "guess": guess})
```

- [ ] **Step 4: Pass resolved sort state to the template**

Replace the template context with:

```python
        {
            "query": q,
            "type": type,
            "sort": sort,
            "direction": direction,
            "results": results,
            "error": error,
        },
```

- [ ] **Step 5: Run the route test to verify sorting behavior passes**

Run: `uv run pytest tests/test_routes.py -k "search_sorts_peers or search_renders_sort_links" -v`

Expected: the order assertions pass, while the header-link assertions still fail until Task 3.

### Task 3: Add sortable peer headers and visual state

**Files:**
- Modify: `src/skald/templates/search.html:27-43`
- Modify: `src/skald/static/style.css:366-415`
- Test: `tests/test_routes.py:after test_search_grab_and_jobs_pages`

- [ ] **Step 1: Replace the combined peer column with sortable headers and cells**

Replace the `Peers` `<th>` and peer `<td>` with the following template markup:

```html
        <th scope="col">
          <a class="sort-link {% if sort == 'seeders' %}is-active{% endif %}"
             href="/search?q={{ query | urlencode }}&amp;type={{ type }}&amp;sort=seeders&amp;direction={% if sort == 'seeders' and direction == 'desc' %}asc{% else %}desc{% endif %}"
             aria-label="Sort by seeders {% if sort == 'seeders' and direction == 'desc' %}ascending{% else %}descending{% endif %}">
            Seeders{% if sort == 'seeders' %} <span aria-hidden="true">{% if direction == 'asc' %}↑{% else %}↓{% endif %}</span>{% endif %}
          </a>
        </th>
        <th scope="col">
          <a class="sort-link {% if sort == 'leechers' %}is-active{% endif %}"
             href="/search?q={{ query | urlencode }}&amp;type={{ type }}&amp;sort=leechers&amp;direction={% if sort == 'leechers' and direction == 'desc' %}asc{% else %}desc{% endif %}"
             aria-label="Sort by leechers {% if sort == 'leechers' and direction == 'desc' %}ascending{% else %}descending{% endif %}">
            Leechers{% if sort == 'leechers' %} <span aria-hidden="true">{% if direction == 'asc' %}↑{% else %}↓{% endif %}</span>{% endif %}
          </a>
        </th>
```

```html
        <td><span class="seeders">&#9650;{{ item.release.seeders }}</span></td>
        <td><span class="leechers">&#9660;{{ item.release.leechers }}</span></td>
```

- [ ] **Step 2: Add sort-link styles beside the table rules**

Add after the `thead th` rule:

```css
.sort-link {
  color: inherit;
  text-decoration: none;
}

.sort-link:hover,
.sort-link:focus-visible {
  color: var(--text);
  text-decoration: underline;
}

.sort-link.is-active {
  color: var(--accent-strong);
}
```

- [ ] **Step 3: Run the targeted route test**

Run: `uv run pytest tests/test_routes.py -k "search_sorts_peers or search_renders_sort_links" -v`

Expected: PASS.

- [ ] **Step 4: Run the full route suite**

Run: `uv run pytest tests/test_routes.py -v`

Expected: PASS with no regressions in grab, authentication, jobs, or lifecycle route behavior.

- [ ] **Step 5: Manually verify the running UI**

Open `http://127.0.0.1:8000/search?q=matrix&type=movie` and verify seeders are initially descending. Click the Seeders header and verify ascending order; click Leechers and verify descending order, URL state, indicator, keyboard focus, and narrow-screen horizontal scroll.
