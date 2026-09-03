# TV Season and Episode Subscription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Target TV seasons and episodes and download only requested files from matching torrent packs.

**Architecture:** Persist TV scopes and match parsed release episode sets. Add paused-torrent file-priority operations to qBittorrent; auto-grab only resumes after target file indexes are selected.

**Tech Stack:** FastAPI, SQLModel/SQLite, GuessIt, qBittorrent Web API, pytest.

---

### Task 1: TV scope persistence and matching

**Files:** `src/skald/models.py`, `src/skald/db.py`, `src/skald/subscriptions.py`, `tests/test_subscriptions.py`

- [ ] Write failing tests for series mode, selected seasons, exact episodes, specials, and packs containing requested episodes.
- [ ] Run `uv run pytest tests/test_subscriptions.py -q`; expect missing scope models/matcher.
- [ ] Add durable scope records using TMDB series/season/episode IDs and coordinates. Implement parser-based matching with existing normalized episode sets.
- [ ] Run focused tests green and commit `feat: add tv subscription scopes`.

### Task 2: qBittorrent selective files

**Files:** `src/skald/qbittorrent.py`, `src/skald/services/grab.py`, `tests/test_qbittorrent.py`, `tests/test_grab_service.py`

- [ ] Write failing tests for add-paused, metadata polling, file-index priorities, and resume.
- [ ] Run focused tests; expect missing APIs.
- [ ] Add paused torrent creation, files lookup, priority 0/1 updates by file index, and resume. Keep torrent paused and raise a typed error when no target file exists.
- [ ] Run focused tests green and commit `feat: select torrent files for episodes`.

### Task 3: Scoped auto-grab

**Files:** `src/skald/subscriptions.py`, `src/skald/worker.py`, `tests/test_subscriptions.py`, `tests/test_worker.py`

- [ ] Write failing tests proving scoped TV auto-grab chooses only required files and does not resume on missing targets.
- [ ] Run focused tests red.
- [ ] Connect profile matching, scope matching, and the selective Grab service; preserve movie behavior and durable deduplication.
- [ ] Run focused tests green and commit `feat: auto grab targeted tv episodes`.

### Task 4: TV detail UI

**Files:** `src/skald/tmdb.py`, `src/skald/routes/subscriptions.py`, `src/skald/templates/subscription_detail.html`, `src/skald/templates/subscriptions.html`, `src/skald/static/style.css`, `tests/test_subscriptions.py`

- [ ] Write failing route tests for series metadata, season/episode selection, and edit actions.
- [ ] Run tests red.
- [ ] Add TMDB season endpoints, protected detail routes, and the designer-owned season/episode UI.
- [ ] Run route/UI regression tests green and commit `feat: configure tv subscription scopes`.

### Task 5: Full verification

- [ ] Run `uv run python -m compileall src && uv run pytest -q && git diff --check`.
- [ ] Verify a selected S02E03 pack enables only its matching qBittorrent file index; verify the torrent remains paused when absent.
