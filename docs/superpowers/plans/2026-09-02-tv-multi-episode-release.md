# TV Multi-Episode Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept, persist, display, and safely organize TV releases that contain a range or non-contiguous set of episodes.

**Architecture:** Introduce one small episode-set utility that normalizes GuessIt values, parses form input, serializes persisted values, and formats labels. `MediaJob.episode` remains the scalar first episode for compatibility; a nullable JSON `episode_set` column retains only multi-episode metadata. Search and job routes exchange scalar start values plus validated episode-set text, while the worker uses the saved set only when naming one combined video.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, GuessIt, Jinja2, pytest.

---

## File structure

- Create: `src/skald/episodes.py` — canonical episode-set normalization, parsing, storage serialization, and display/file formatting.
- Modify: `src/skald/parser.py` — normalize GuessIt collection values to scalar start plus an ordered set.
- Modify: `src/skald/models.py` and `src/skald/db.py` — add the nullable `episode_set` storage column and additive SQLite migration.
- Modify: `src/skald/routes/search.py` and `src/skald/templates/search.html` — expose formatted ranges and post safe scalar/set values.
- Modify: `src/skald/routes/jobs.py` and `src/skald/templates/job_detail.html` — validate/persist/retry multi-episode values and display them accurately.
- Modify: `src/skald/organizer.py` and `src/skald/worker.py` — give one combined video a deterministic multi-episode target name; retain source-marker pack behavior.
- Create: `tests/test_episodes.py` — canonical episode-set unit coverage.
- Modify: `tests/test_parser.py`, `tests/test_models.py`, `tests/test_routes.py`, `tests/test_organizer.py`, and `tests/test_worker.py` — parser, persistence, request, rendering, and organization coverage.

Do not commit unless the user explicitly requests a commit.

### Task 1: Define canonical episode-set behavior

**Files:**
- Create: `src/skald/episodes.py`
- Create: `tests/test_episodes.py`
- Modify: `tests/test_parser.py:1-18`

- [ ] **Step 1: Write failing normalization and formatting tests**

```python
import pytest

from skald.episodes import (
    format_episode_label,
    parse_episode_set_input,
    serialize_episode_set,
)


def test_parse_episode_set_input_sorts_deduplicates_and_compresses_ranges():
    assert parse_episode_set_input("6, 1-3, 3") == (1, 2, 3, 6)
    assert serialize_episode_set((1, 2, 3, 6)) == "[1,2,3,6]"
    assert format_episode_label((1, 2, 3, 6)) == "E01-E03-E06"


@pytest.mark.parametrize("value", ["", "0", "2-1", "1,,2", "not episodes"])
def test_parse_episode_set_input_rejects_malformed_or_non_positive_values(value):
    with pytest.raises(ValueError):
        parse_episode_set_input(value)
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `rtk uv run pytest tests/test_episodes.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'skald.episodes'`.

- [ ] **Step 3: Implement the focused utility**

Create `src/skald/episodes.py` with these public functions:

```python
import json
import re
from collections.abc import Iterable


def normalize_episode_set(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, int) else tuple(value)
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in values):
        raise ValueError("episodes must be positive integers")
    return tuple(sorted(set(values)))


def parse_episode_set_input(value: str) -> tuple[int, ...]:
    # Accept JSON storage (`[1,2,3]`) or human input (`1-3,6`), and return
    # sorted, deduplicated positive integers. Reject empty tokens and reversed ranges.
    ...


def serialize_episode_set(episodes: tuple[int, ...]) -> str:
    return json.dumps(list(episodes), separators=(",", ":"))


def deserialize_episode_set(value: str | None) -> tuple[int, ...]:
    return () if value is None else parse_episode_set_input(value)


def format_episode_label(episodes: tuple[int, ...]) -> str:
    # `E05`, `E01-E06`, or `E01-E03,E05`; runs are inclusive and two-digit.
    ...
```

Use a full-string `re.fullmatch(r"(\d+)(?:-(\d+))?")` for each comma-separated
human token. For JSON input, require a JSON list before passing it to
`normalize_episode_set`; do not accept arbitrary JSON scalars.

- [ ] **Step 4: Add real GuessIt regression coverage**

Append to `tests/test_parser.py`:

```python
def test_parse_tv_multi_episode_release_uses_first_episode_and_preserves_set():
    guess = parse_release("Black.Mirror.S07E01-06.1080p.WEB-DL")

    assert guess["season"] == 7
    assert guess["episode"] == 1
    assert guess["episode_set"] == (1, 2, 3, 4, 5, 6)
```

- [ ] **Step 5: Normalize parser output and verify GREEN**

Replace the raw `episode` return in `src/skald/parser.py` with normalized data:

```python
    episodes = normalize_episode_set(guess.get("episode"))
    return {
        # existing title/year/season/media_type entries
        "episode": episodes[0] if episodes else None,
        "episode_set": episodes,
    }
```

Import `normalize_episode_set`. Preserve scalar behavior: `S01E05` returns
`episode == 5` and `episode_set == (5,)`.

Run: `rtk uv run pytest tests/test_episodes.py tests/test_parser.py -v`

Expected: PASS.

### Task 2: Add nullable multi-episode persistence

**Files:**
- Modify: `src/skald/models.py:41-63`
- Modify: `src/skald/db.py:12-78`
- Modify: `tests/test_models.py:10-58,78-135`

- [ ] **Step 1: Write the failing schema compatibility test**

Add a legacy-table test that creates `mediajob` without `episode_set`, calls
`migrate_schema(engine)`, and verifies both raw schema and ORM read-back:

```python
with engine.connect() as connection:
    columns = {column[1] for column in connection.exec_driver_sql("PRAGMA table_info(mediajob)")}
    assert "episode_set" in columns
    assert connection.exec_driver_sql("SELECT episode_set FROM mediajob WHERE id = 1").scalar() is None

with Session(engine) as session:
    assert session.get(MediaJob, 1).episode_set is None
```

- [ ] **Step 2: Verify RED**

Run: `rtk uv run pytest tests/test_models.py -k episode_set -v`

Expected: FAIL because the column/model field does not exist.

- [ ] **Step 3: Add the model field and additive migration**

Place this immediately after `episode` in `MediaJob`:

```python
    episode_set: Optional[str] = None
```

In `migrate_schema()`, after collecting `column_names`, add:

```python
        if "episode_set" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN episode_set VARCHAR")
```

The column must remain nullable; do not backfill existing scalar jobs with
`[episode]`.

- [ ] **Step 4: Verify GREEN**

Run: `rtk uv run pytest tests/test_models.py -v`

Expected: PASS.

### Task 3: Render safe multi-episode search forms

**Files:**
- Modify: `src/skald/routes/search.py:16-67`
- Modify: `src/skald/templates/search.html:53-95`
- Modify: `tests/test_routes.py:159-290`

- [ ] **Step 1: Write the failing complete-pack form test**

Stub `search_routes.parse_release` with normalized multi-episode data and
assert it is complete and its form posts a scalar start plus JSON set:

```python
lambda _: {
    "title": "Black Mirror", "year": 2025, "season": 7,
    "episode": 1, "episode_set": (1, 2, 3, 4, 5, 6),
}

assert "data-grab-review-toggle" not in response.text
assert 'name="episode" value="1"' in response.text
assert 'name="episode_set" value="[1,2,3,4,5,6]"' in response.text
assert 'name="episode" value="[1, 2, 3, 4, 5, 6]"' not in response.text
assert "E01-E06" in response.text
```

Add an incomplete TV test with `episode=None` and `episode_set=()` that asserts
the review form exposes `episode_set` as a required text control, alongside the
existing required `title`, `season`, and numeric `episode` controls.

- [ ] **Step 2: Verify RED**

Run: `rtk uv run pytest tests/test_routes.py -k 'multi_episode or incomplete_metadata' -v`

Expected: FAIL because search has no serialized set or formatted label.

- [ ] **Step 3: Add result-level presentation values**

In `search.py`, import `format_episode_label` and `serialize_episode_set`. For
each result, compute:

```python
episode_set = guess.get("episode_set", ())
episode_set_value = serialize_episode_set(episode_set) if len(episode_set) > 1 else ""
episode_label = format_episode_label(episode_set) if episode_set else ""
```

Attach these values to the result dictionary. Keep `needs_metadata_review()`
based on scalar `episode`, so a normalized multi-episode parse is complete.

- [ ] **Step 4: Update the form without changing its ownership boundary**

In both TV branches of `search.html`, keep exactly one numeric `episode` input.
For a non-empty multi-episode set, add:

```html
<input type="hidden" name="episode_set" value="{{ item.episode_set_value }}">
<span class="grab-episode-set">{{ item.episode_label }}</span>
```

For the review branch, replace the hidden set field with a visible required
text input only when a set is already known:

```html
<input type="text" name="episode_set" value="{{ item.episode_set_value }}"
       placeholder="Episodes (e.g. 1-6)"{% if item.episode_set_value %} required{% endif %}>
```

Do not emit an `episode_set` control for a normal single-episode result. Keep
the existing UI structure and review disclosure behavior intact.

- [ ] **Step 5: Verify GREEN**

Run: `rtk uv run pytest tests/test_routes.py -k 'multi_episode or incomplete_metadata' -v`

Expected: PASS.

### Task 4: Validate, persist, retry, and display multi-episode jobs

**Files:**
- Modify: `src/skald/routes/jobs.py:59-114,187-192,245-278`
- Modify: `src/skald/templates/job_detail.html:44-48,70-89`
- Modify: `tests/test_routes.py:159-290`

- [ ] **Step 1: Write failing request and retry tests**

Add route tests that:

```python
# /grab accepts a range, persists the scalar start and JSON set, and calls qBittorrent once.
response = client.post("/grab", data={**tv_metadata, "episode": 1, "episode_set": "1-6"})
assert response.status_code == 303
assert job.episode == 1
assert job.episode_set == "[1,2,3,4,5,6]"

# malformed/reversed/zero set fails before side effects.
response = client.post("/grab", data={**tv_metadata, "episode": 1, "episode_set": "6-1"})
assert response.status_code == 422
assert qbit.add_calls == []
```

Add a retry test for a `NEEDS_ATTENTION` TV job with `episode_set="[1,2,3]"`
that submits `episode_set="1-3"` and verifies saved normalized JSON, reset
error, and `COMPLETED` status. Assert job detail shows `S7E01-E06` and its retry
form preserves `episode_set` as `1-6`.

- [ ] **Step 2: Verify RED**

Run: `rtk uv run pytest tests/test_routes.py -k 'episode_set or multi_episode' -v`

Expected: FAIL because route signatures/model persistence/template display do
not yet support `episode_set`.

- [ ] **Step 3: Centralize route validation before side effects**

Add a private helper in `routes/jobs.py` that receives media type, scalar
episode, and optional form `episode_set`, then returns canonical storage text:

```python
def _validated_episode_set(media_type: str, episode: int | None, episode_set: str | None) -> str | None:
    if not episode_set:
        return None
    if media_type != MediaType.TV.value:
        raise HTTPException(status_code=422, detail="episode set is only valid for TV")
    try:
        episodes = parse_episode_set_input(episode_set)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid episode set: {exc}") from exc
    if episode is None or episodes[0] != episode:
        raise HTTPException(status_code=422, detail="episode must equal the first episode in the set")
    return serialize_episode_set(episodes) if len(episodes) > 1 else None
```

Call it after existing scalar TV validation and before `qbit.add_torrent()`.
Accept `episode_set: Optional[str] = Form(None)` in both `grab()` and
`retry_job()`, assign the returned value to `job.episode_set`, and reuse the
same title/type-required validation in retry before mutating the job.

- [ ] **Step 4: Supply template display/input values**

In `job_detail()`, deserialize `job.episode_set` and pass:

```python
episode_label = format_episode_label(episodes or ((job.episode,) if job.episode else ()))
episode_set_input = format_episode_input(episodes) if episodes else ""
```

Add `format_episode_input()` to `episodes.py`; it returns compact human input
such as `1-6` or `1-3,5`. Render `S{{ job.season }}{{ episode_label }}` in the
detail field and a prefilled `name="episode_set"` text field in the TV retry
form when it has a saved multi-episode set. Preserve the single-episode display
and form unchanged.

- [ ] **Step 5: Verify GREEN**

Run: `rtk uv run pytest tests/test_routes.py -k 'episode_set or multi_episode or grab_rejects' -v`

Expected: PASS.

### Task 5: Name combined videos from saved episode sets

**Files:**
- Modify: `src/skald/organizer.py:101-126`
- Modify: `src/skald/worker.py:150-169`
- Modify: `tests/test_organizer.py:24-48`
- Modify: `tests/test_worker.py:258-329`

- [ ] **Step 1: Write failing target-path tests**

```python
def test_tv_target_path_names_contiguous_multi_episode_video():
    assert tv_target_path("/library/tv", "Black Mirror", 7, 1, ".mkv", (1, 2, 3, 4, 5, 6)) == (
        Path("/library/tv/Black Mirror/Season 07/Black Mirror - S07E01-E06.mkv")
    )


def test_tv_target_path_names_non_contiguous_multi_episode_video():
    assert tv_target_path("/library/tv", "Show", 1, 1, ".mkv", (1, 3, 5)) == (
        Path("/library/tv/Show/Season 01/Show - S01E01-E03-E05.mkv")
    )
```

Add an async worker test with one video file and
`episode_set="[1,2,3,4,5,6]"`; assert it becomes organized at
`Black Mirror - S07E01-E06.mkv`. Retain the existing two-video pack test and
assert its targets remain separate `E01` and `E02` files despite any job-level
episode set.

- [ ] **Step 2: Verify RED**

Run: `rtk uv run pytest tests/test_organizer.py tests/test_worker.py -k 'multi_episode or tv_target_path or tv_pack' -v`

Expected: FAIL because `tv_target_path()` accepts no episode-set argument and
the worker does not deserialize persisted data.

- [ ] **Step 3: Extend scalar target naming only**

Change the target helper signature to:

```python
def tv_target_path(
    tv_root: str, series: str, season: int, episode: int, source_ext: str,
    episode_set: tuple[int, ...] = (),
) -> Path:
    episode_label = format_episode_label(episode_set or (episode,))
    return Path(tv_root) / series / f"Season {season:02d}" / (
        f"{series} - S{season:02d}{episode_label}{source_ext}"
    )
```

Import `format_episode_label`. Leave every `build_tv_pack_targets()` call
unchanged, so the source filename remains authoritative for multiple files.
In the scalar-TV branch of `organize_job()`, pass
`deserialize_episode_set(job.episode_set)` to `tv_target_path()`.

- [ ] **Step 4: Verify GREEN**

Run: `rtk uv run pytest tests/test_organizer.py tests/test_worker.py -k 'multi_episode or tv_target_path or tv_pack' -v`

Expected: PASS.

### Task 6: Run regression verification

**Files:**
- Test: `tests/test_parser.py`
- Test: `tests/test_episodes.py`
- Test: `tests/test_models.py`
- Test: `tests/test_routes.py`
- Test: `tests/test_organizer.py`
- Test: `tests/test_worker.py`

- [ ] **Step 1: Check formatting and run the focused feature suite**

Run:

```bash
rtk git diff --check
rtk uv run pytest tests/test_episodes.py tests/test_parser.py tests/test_models.py tests/test_routes.py tests/test_organizer.py tests/test_worker.py -v
```

Expected: zero whitespace errors and all selected tests pass.

- [ ] **Step 2: Run the complete suite**

Run: `rtk uv run pytest -v`

Expected: all tests pass. Investigate any failure before reporting completion.
