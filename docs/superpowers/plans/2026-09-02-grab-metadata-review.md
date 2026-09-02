# Grab Metadata Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide correctly parsed grab metadata, while marking incomplete parsing and revealing its editable controls on demand.

**Architecture:** `search.py` determines whether a result needs review from its requested media type and parsed metadata. `search.html` renders a compact hidden-value Grab form for complete results or an initially collapsed review control for incomplete ones. A small browser module opens only the selected review region, updates accessibility state, and focuses title editing.

**Tech Stack:** Python 3, FastAPI, Jinja2, vanilla ES modules, pytest, Node’s built-in test runner, CSS.

---

## File structure

- `src/skald/routes/search.py` — owns deterministic metadata-completeness rules and passes `needs_review` per result.
- `src/skald/templates/search.html` — conditionally renders compact grab controls or marked, collapsed editable metadata.
- `src/skald/static/grab_metadata_review.mjs` — owns the button-driven disclosure interaction.
- `src/skald/static/style.css` — visually distinguishes the review status and disclosure control without changing the existing complete-result Grab button.
- `tests/test_routes.py` — covers complete movie/TV markup and incomplete movie/TV review markup using deterministic parser outputs.
- `tests/grab_metadata_review.test.mjs` — covers the disclosure controller without a browser dependency.

### Task 1: Write failing complete/incomplete rendering tests

**Files:**
- Modify: `tests/test_routes.py:1-15, after test_search_grab_and_jobs_pages`

- [ ] **Step 1: Import the search route module for deterministic parser stubbing**

Add next to the existing route import:

```python
from skald.routes import search as search_routes
```

- [ ] **Step 2: Add a complete-movie rendering test**

```python
def test_search_hides_grab_metadata_when_movie_parse_is_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "complete_movie.db"))
    monkeypatch.setattr(
        search_routes,
        "parse_release",
        lambda _: {"title": "The Matrix", "year": 1999, "season": None, "episode": None},
    )
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        response = client.get("/search", params={"q": "matrix", "type": "movie"})

    assert response.status_code == 200
    assert 'name="title" value="The Matrix"' in response.text
    assert 'name="year" value="1999"' in response.text
    assert "data-grab-review-toggle" not in response.text
    assert 'type="text" name="title"' not in response.text
```

- [ ] **Step 3: Add incomplete movie and TV rendering tests**

```python
@pytest.mark.parametrize(
    ("media_type", "guess", "expected_names"),
    [
        (
            "movie",
            {"title": "The Matrix", "year": None, "season": None, "episode": None},
            ["title", "year"],
        ),
        (
            "tv",
            {"title": "Breaking Bad", "year": None, "season": 1, "episode": None},
            ["title", "year", "season", "episode"],
        ),
    ],
)
def test_search_marks_incomplete_metadata_for_review(tmp_path, monkeypatch, media_type, guess, expected_names):
    monkeypatch.setenv("DB_PATH", str(tmp_path / f"incomplete_{media_type}.db"))
    monkeypatch.setattr(search_routes, "parse_release", lambda _: guess)
    app = create_app()

    with TestClient(app) as client:
        app.state.indexer = FakeIndexer()
        response = client.get("/search", params={"q": "matrix", "type": media_type})

    assert response.status_code == 200
    assert "Needs review" in response.text
    assert 'data-grab-review-toggle' in response.text
    assert 'aria-expanded="false"' in response.text
    assert 'data-grab-review-fields hidden' in response.text
    for name in expected_names:
        assert f'name="{name}"' in response.text
```

- [ ] **Step 4: Run the route tests to verify they fail**

Run: `uv run pytest tests/test_routes.py -k "hides_grab_metadata or marks_incomplete_metadata" -v`

Expected: FAIL because no result carries `needs_review`, the current template always renders editable inputs, and no review control exists.

### Task 2: Add the metadata-completeness rule

**Files:**
- Modify: `src/skald/routes/search.py:12-56`
- Test: `tests/test_routes.py:after test_search_grab_and_jobs_pages`

- [ ] **Step 1: Add a focused completeness helper**

Add above `search()`:

```python
def needs_metadata_review(guess: dict, media_type: str) -> bool:
    required_fields = ("title", "season", "episode") if media_type == "tv" else ("title", "year")
    return any(guess[field] is None for field in required_fields)
```

- [ ] **Step 2: Attach review state to each result**

Replace the current result append statement with:

```python
                results.append(
                    {
                        "release": release,
                        "guess": guess,
                        "needs_review": needs_metadata_review(guess, type),
                    }
                )
```

- [ ] **Step 3: Run the route tests**

Run: `uv run pytest tests/test_routes.py -k "hides_grab_metadata or marks_incomplete_metadata" -v`

Expected: the tests continue to fail only because the template has not used `needs_review` yet.

### Task 3: Render compact and review-state grab controls

**Files:**
- Modify: `src/skald/templates/search.html:61-73`
- Modify: `src/skald/static/style.css:417-447`
- Test: `tests/test_routes.py:after test_search_grab_and_jobs_pages`

- [ ] **Step 1: Replace the always-visible metadata controls**

Inside the existing `/grab` form, retain the three existing hidden fields for release title, download URL, and media type. Replace the remaining fields and button with:

```html
            {% if item.needs_review %}
            {% set review_id = "grab-review-" ~ loop.index0 %}
            <div class="grab-review" data-grab-review>
              <span class="grab-review-status">Needs review</span>
              <button type="button" class="grab-review-toggle" data-grab-review-toggle
                      aria-expanded="false" aria-controls="{{ review_id }}">Review metadata</button>
              <div id="{{ review_id }}" class="grab-review-fields" data-grab-review-fields hidden>
                <input type="text" name="title" value="{{ item.guess.title or '' }}" placeholder="Title">
                <input type="number" name="year" value="{{ item.guess.year or '' }}" placeholder="Year">
                {% if type == "tv" %}
                <input type="number" name="season" value="{{ item.guess.season or '' }}" placeholder="S">
                <input type="number" name="episode" value="{{ item.guess.episode or '' }}" placeholder="E">
                {% endif %}
                <button type="submit">Grab</button>
              </div>
            </div>
            {% else %}
            <input type="hidden" name="title" value="{{ item.guess.title }}">
            <input type="hidden" name="year" value="{{ item.guess.year }}">
            {% if type == "tv" %}
            <input type="hidden" name="season" value="{{ item.guess.season }}">
            <input type="hidden" name="episode" value="{{ item.guess.episode }}">
            {% endif %}
            <button type="submit">Grab</button>
            {% endif %}
```

- [ ] **Step 2: Add styles for the review status and its expanded controls**

Add below the existing `.grab-form` rule:

```css
.grab-review {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.grab-review-status {
  color: #ffb4bb;
  font-size: 12px;
  font-weight: 600;
}

.grab-review-fields {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  width: 100%;
}
```

- [ ] **Step 3: Load the review module only when result rows are present**

Add this block after `{% endblock %}` for content:

```html
{% block scripts %}
{% if results %}<script type="module" src="/static/grab_metadata_review.mjs"></script>{% endif %}
{% endblock %}
```

- [ ] **Step 4: Run the route tests**

Run: `uv run pytest tests/test_routes.py -k "hides_grab_metadata or marks_incomplete_metadata" -v`

Expected: PASS.

### Task 4: Implement and test button disclosure

**Files:**
- Create: `src/skald/static/grab_metadata_review.mjs`
- Create: `tests/grab_metadata_review.test.mjs`

- [ ] **Step 1: Write the failing Node test using minimal DOM doubles**

Create `tests/grab_metadata_review.test.mjs`:

```javascript
import assert from "node:assert/strict";
import test from "node:test";

import { initializeGrabMetadataReview } from "../src/skald/static/grab_metadata_review.mjs";

test("opens a review region and focuses its title input", () => {
  const title = { focused: false, focus() { this.focused = true; } };
  const fields = { hidden: true, querySelector(selector) { return selector === '[name="title"]' ? title : null; } };
  const button = {
    attributes: { "aria-expanded": "false" },
    addEventListener(_event, listener) { this.listener = listener; },
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    closest() { return review; },
  };
  const review = { querySelector(selector) { return selector === "[data-grab-review-fields]" ? fields : null; } };
  const root = { querySelectorAll(selector) { return selector === "[data-grab-review-toggle]" ? [button] : []; } };

  initializeGrabMetadataReview(root);
  button.listener();

  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal(fields.hidden, false);
  assert.equal(title.focused, true);
});
```

- [ ] **Step 2: Run the Node test to verify it fails**

Run: `node --test tests/grab_metadata_review.test.mjs`

Expected: FAIL because `grab_metadata_review.mjs` does not exist.

- [ ] **Step 3: Implement the disclosure module**

Create `src/skald/static/grab_metadata_review.mjs`:

```javascript
export function initializeGrabMetadataReview(root = document) {
  root.querySelectorAll("[data-grab-review-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const review = button.closest("[data-grab-review]");
      const fields = review?.querySelector("[data-grab-review-fields]");
      if (!fields) return;

      fields.hidden = false;
      button.setAttribute("aria-expanded", "true");
      fields.querySelector('[name="title"]')?.focus();
    });
  });
}

if (typeof document !== "undefined") initializeGrabMetadataReview();
```

- [ ] **Step 4: Run the Node test to verify it passes**

Run: `node --test tests/grab_metadata_review.test.mjs`

Expected: PASS.

- [ ] **Step 5: Run all relevant verification**

Run: `uv run pytest tests/test_routes.py -v && node --test tests/grab_metadata_review.test.mjs`

Expected: PASS.

- [ ] **Step 6: Manually verify the running page**

Search for a known complete release and confirm only Grab is visible. Search for or stub an incomplete result, confirm `Needs review` and `Review metadata` appear, then activate the button by keyboard and verify fields appear in the same row, focus moves to Title, and Grab submits corrected metadata.
