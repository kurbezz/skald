# Search Size Sorting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users sort search results by release size, initially showing the largest releases first.

**Architecture:** Extend the route’s existing `sort` allowlist with the `ReleaseResult.size_bytes` attribute. Replace the static Size table header with the same server-rendered sort-link pattern used by Seeders and Leechers; no JavaScript or new CSS is needed.

**Tech Stack:** Python 3, FastAPI, Jinja2 templates, pytest.

---

## File structure

- `tests/test_routes.py` — makes fixture sizes distinct and verifies size ordering, link targets, and ARIA sort state.
- `src/skald/routes/search.py` — accepts `size_bytes` as an allowed sort key.
- `src/skald/templates/search.html` — makes the Size header a server-rendered sortable column.

### Task 1: Add size-sorting red tests

**Files:**
- Modify: `tests/test_routes.py:31-58`
- Modify: `tests/test_routes.py:129-208`

- [ ] **Step 1: Make the multi-result fixture sizes distinct**

In `MultiResultIndexer`, change the three `size_bytes` values to:

```python
# Seeder.High
size_bytes=2_000_000_000,

# Leecher.High
size_bytes=3_000_000_000,

# Middle.Peers
size_bytes=1_000_000_000,
```

- [ ] **Step 2: Add size-order cases to `test_search_sorts_peers`**

Add these two entries to its parameter list:

```python
(
    {"sort": "size_bytes", "direction": "desc"},
    ["Leecher.High", "Seeder.High", "Middle.Peers"],
),
(
    {"sort": "size_bytes", "direction": "asc"},
    ["Middle.Peers", "Seeder.High", "Leecher.High"],
),
```

- [ ] **Step 3: Add a Size-link assertion**

In `test_search_renders_sort_links`, add:

```python
assert 'href="/search?q=matrix&amp;type=movie&amp;sort=size_bytes&amp;direction=desc"' in response.text
```

- [ ] **Step 4: Add a failing active-Size ARIA test**

Add this test below `test_search_renders_active_sort_header_aria`:

```python
@pytest.mark.parametrize(
    ("params", "expected_aria_sort"),
    [
        pytest.param(
            {"sort": "size_bytes", "direction": "desc"},
            "descending",
            id="size-descending",
        ),
        pytest.param(
            {"sort": "size_bytes", "direction": "asc"},
            "ascending",
            id="size-ascending",
        ),
    ],
)
def test_search_renders_active_size_sort_header_aria(
    tmp_path, monkeypatch, params, expected_aria_sort
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search_size_header_aria.db"))
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = MultiResultIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie", **params})

    assert response.status_code == 200
    size_header = re.search(
        r"<th\b[^>]*>\s*<a\b[^>]*>[^<]*Size(?:[^<]|<(?!/th\b))*</th>",
        response.text,
        re.DOTALL,
    )
    seeders_header = re.search(
        r"<th\b[^>]*>\s*<a\b[^>]*>[^<]*Seeders(?:[^<]|<(?!/th\b))*</th>",
        response.text,
        re.DOTALL,
    )
    assert size_header is not None
    assert seeders_header is not None
    assert f'aria-sort="{expected_aria_sort}"' in size_header.group()
    assert "aria-sort" not in seeders_header.group()
```

- [ ] **Step 5: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k "search_sorts_peers or search_renders_sort_links or search_renders_active_size_sort_header_aria" -v`

Expected: the new size-order cases fail because `size_bytes` is rejected; the Size link and ARIA assertions fail because Size is static.

### Task 2: Allow sorting by size

**Files:**
- Modify: `src/skald/routes/search.py:12-13`
- Test: `tests/test_routes.py:129-208`

- [ ] **Step 1: Extend the route allowlist**

Replace the current field set with:

```python
SORT_FIELDS = {"seeders", "leechers", "size_bytes"}
```

- [ ] **Step 2: Run ordering tests**

Run: `uv run pytest tests/test_routes.py -k "search_sorts_peers" -v`

Expected: all peer and size ordering cases PASS. Header-link and ARIA tests for Size still fail until Task 3.

### Task 3: Make the Size header sortable

**Files:**
- Modify: `src/skald/templates/search.html:29-30`
- Test: `tests/test_routes.py:162-208`

- [ ] **Step 1: Replace the static Size header**

Replace `<th>Size</th>` with:

```html
        <th scope="col"{% if sort == 'size_bytes' %} aria-sort="{% if direction == 'asc' %}ascending{% else %}descending{% endif %}"{% endif %}>
          <a class="sort-link {% if sort == 'size_bytes' %}is-active{% endif %}"
             href="/search?q={{ query | urlencode }}&amp;type={{ type }}&amp;sort=size_bytes&amp;direction={% if sort == 'size_bytes' and direction == 'desc' %}asc{% else %}desc{% endif %}"
             aria-label="Sort by size {% if sort == 'size_bytes' and direction == 'desc' %}ascending{% else %}descending{% endif %}">
            Size{% if sort == 'size_bytes' %} <span aria-hidden="true">{% if direction == 'asc' %}↑{% else %}↓{% endif %}</span>{% endif %}
          </a>
        </th>
```

- [ ] **Step 2: Run all size-sort tests**

Run: `uv run pytest tests/test_routes.py -k "search_sorts_peers or search_renders_sort_links or search_renders_active_size_sort_header_aria" -v`

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -v`

Expected: PASS with no regressions.

- [ ] **Step 4: Manually verify the running UI**

Open `http://127.0.0.1:8000/search?q=matrix&type=movie`, click Size, and verify the URL contains `sort=size_bytes&direction=desc`; click Size again and verify it becomes `direction=asc`, the arrow changes, and the Size header’s `aria-sort` matches the active direction.
