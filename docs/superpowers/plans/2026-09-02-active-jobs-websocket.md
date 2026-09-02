# Active Jobs WebSocket Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update `/jobs?tab=active` in place from one WebSocket connection as jobs progress, enter, or leave the active set.

**Architecture:** A collection WebSocket at `/ws/jobs/active` sends changed authoritative snapshots of active jobs and the completed count. The active-list script reconciles keyed table rows and tab counts without reloading; the server-rendered template remains the baseline and fallback.

**Tech Stack:** FastAPI WebSocket, SQLModel, Jinja2, browser-native JavaScript, pytest/TestClient.

---

## File structure

- `src/skald/routes/jobs.py` — builds active-job snapshots and exposes the collection WebSocket before the dynamic job WebSocket.
- `src/skald/templates/jobs.html` — provides keyed active-list DOM hooks, row template, empty-state toggle, live region, and conditional script include.
- `src/skald/static/active_jobs.js` — opens one socket and reconciles active-list snapshots in place.
- `src/skald/static/style.css` — adds only a reusable visually-hidden utility for the live region.
- `tests/test_routes.py` — proves socket payload transitions and template contracts.

### Task 1: Active-list WebSocket route

**Files:**
- Modify: `tests/test_routes.py:154-185`
- Modify: `src/skald/routes/jobs.py:17-27,145-171`

- [ ] **Step 1: Write the failing socket-stream test**

  Add this test after `test_job_detail_websocket_streams_status`:

  ```python
  def test_active_jobs_websocket_streams_changed_snapshot(tmp_path, monkeypatch):
      monkeypatch.setenv("DB_PATH", str(tmp_path / "active-ws.db"))
      app = create_app()

      with TestClient(app) as client:
          with Session(app.state.engine) as session:
              job = MediaJob(
                  type=MediaType.MOVIE,
                  title="Active WS Movie",
                  year=2020,
                  release_title="Active.WS.Movie.2020",
                  qbit_hash="fakehash",
                  category="skald-movie",
                  status=JobStatus.DOWNLOADING,
                  progress=0.25,
              )
              session.add(job)
              session.commit()
              session.refresh(job)
              job_id = job.id

          with client.websocket_connect("/ws/jobs/active") as websocket:
              initial = websocket.receive_json()
              assert initial == {
                  "jobs": [
                      {
                          "id": job_id,
                          "type": "movie",
                          "title": "Active WS Movie",
                          "status": "downloading",
                          "progress": 0.25,
                      }
                  ],
                  "completed_count": 0,
              }

              with Session(app.state.engine) as session:
                  changed = session.get(MediaJob, job_id)
                  changed.status = JobStatus.ORGANIZED
                  session.add(changed)
                  session.commit()

              assert websocket.receive_json() == {"jobs": [], "completed_count": 1}
  ```

- [ ] **Step 2: Run the focused test and confirm RED**

  Run: `uv run pytest tests/test_routes.py::test_active_jobs_websocket_streams_changed_snapshot -v`

  Expected: FAIL because `/ws/jobs/active` is captured by the integer dynamic route or does not exist.

- [ ] **Step 3: Add the snapshot helper and collection WebSocket**

  In `src/skald/routes/jobs.py`, add a helper immediately after the status tuples:

  ```python
  def active_jobs_payload(engine) -> dict:
      with get_session(engine) as session:
          all_jobs = session.exec(select(MediaJob).order_by(MediaJob.created_at.desc())).all()

      active_jobs = [job for job in all_jobs if job.status in ACTIVE_TAB_STATUSES]
      completed_count = sum(job.status in COMPLETED_TAB_STATUSES for job in all_jobs)
      return {
          "jobs": [
              {
                  "id": job.id,
                  "type": job.type.value,
                  "title": job.title,
                  "status": job.status.value,
                  "progress": job.progress,
              }
              for job in active_jobs
          ],
          "completed_count": completed_count,
      }
  ```

  Declare this route immediately before `@router.websocket("/ws/jobs/{job_id}")`:

  ```python
  @router.websocket("/ws/jobs/active")
  async def active_jobs_ws(websocket: WebSocket):
      await websocket.accept()
      last_payload = None
      try:
          while True:
              payload = active_jobs_payload(websocket.app.state.engine)
              if payload != last_payload:
                  await websocket.send_json(payload)
                  last_payload = payload
              await asyncio.sleep(2)
      except WebSocketDisconnect:
          pass
  ```

  Do not change the existing `/ws/jobs/{job_id}` handler.

- [ ] **Step 4: Run the focused test and confirm GREEN**

  Run: `uv run pytest tests/test_routes.py::test_active_jobs_websocket_streams_changed_snapshot -v`

  Expected: PASS; the second payload arrives after the existing two-second polling interval.

### Task 2: Active-list template contract

**Files:**
- Modify: `tests/test_routes.py:47-81`
- Modify: `src/skald/templates/jobs.html:11-72`
- Modify: `src/skald/static/style.css:641-659`

- [ ] **Step 1: Write the failing template assertions**

  Extend `test_search_grab_and_jobs_pages` after the active/completed requests:

  ```python
      assert 'data-active-jobs' in active_response.text
      assert 'data-active-job-list' in active_response.text
      assert 'data-active-job-template' in active_response.text
      assert 'data-active-count' in active_response.text
      assert 'data-completed-count' in active_response.text
      assert 'active_jobs.js' in active_response.text
      assert 'data-active-jobs' not in completed_response.text
      assert 'active_jobs.js' not in completed_response.text
  ```

- [ ] **Step 2: Run the template test and confirm RED**

  Run: `uv run pytest tests/test_routes.py::test_search_grab_and_jobs_pages -v`

  Expected: FAIL because the active page has no list-specific hooks or script.

- [ ] **Step 3: Make the active tab reconcilable without changing its appearance**

  In `jobs.html`:

  - Add `data-active-count` to the Active count only when `tab == "active"`; add `data-completed-count` to the Completed count under the same condition.
  - For `tab == "active"`, wrap the table panel, empty panel, live region, and row template in `<section data-active-jobs data-ws-url="/ws/jobs/active">`.
  - Render the table panel and the existing active empty-state panel in this section regardless of whether `jobs` is empty. Apply Jinja's `hidden` attribute to the table when no jobs exist and to the empty panel when jobs exist. Preserve the completed-tab conditional exactly as it is.
  - Set `<tbody data-active-job-list>`, each active `<tr data-job-row data-job-id="{{ job.id }}" data-job-status="{{ job.status.value }}">`, scoped badge/label hooks, and scoped progress hooks. Set the progress cell to `role="progressbar"`, `aria-valuemin="0"`, `aria-valuemax="100"`, and server-rendered `aria-valuenow`/`aria-valuetext` values.
  - Add `<p class="visually-hidden" data-active-jobs-live role="status" aria-live="polite" aria-atomic="true"></p>` and a `<template data-active-job-template>` whose row contains the same six current columns and matching data hooks. The script will set every dynamic text and URL with DOM properties.
  - Add `{% block scripts %}{% if tab == "active" %}<script src="/static/active_jobs.js" defer></script>{% endif %}{% endblock %}` after the content block.

  In `style.css`, add:

  ```css
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  ```

- [ ] **Step 4: Run the template test and confirm GREEN**

  Run: `uv run pytest tests/test_routes.py::test_search_grab_and_jobs_pages -v`

  Expected: PASS; only the active response includes the hooks and client script.

### Task 3: In-place snapshot reconciliation

**Files:**
- Create: `src/skald/static/active_jobs.js`
- Modify: `src/skald/templates/jobs.html:11-72`

- [ ] **Step 1: Implement `active_jobs.js` after Tasks 1 and 2 are green**

  Implement one IIFE that exits when `[data-active-jobs]` or `window.WebSocket` is absent. It must:

  - Choose `ws:` or `wss:` from `window.location.protocol` and connect once to `container.dataset.wsUrl`.
  - Ignore non-object payloads and payloads whose `jobs` is not an array.
  - Index rows by `data-job-id`; use the template to create missing rows; use `textContent` for title/type/status, `href = "/jobs/" + id`, and `form.action = "/jobs/" + id + "/delete"`.
  - Clamp progress with `Math.min(1, Math.max(0, Number(job.progress) || 0))`; set fill width to one decimal place, label to a rounded integer percent, and the three ARIA progress values.
  - Remove all prior `badge-*` classes before adding `badge-${job.status}`; patch the scoped status label and row dataset.
  - Append each existing/new row in snapshot order, then remove rows absent from the snapshot. If the active element was removed, focus the active-tab anchor.
  - Set the active count from `jobs.length`, set completed count from a finite `payload.completed_count`, and toggle the table/empty panel. Announce only row additions/removals through the live region.
  - Never call `window.location.reload()` and leave `error`/`close` events silent.

- [ ] **Step 2: Run the focused server and template tests**

  Run: `uv run pytest tests/test_routes.py::test_active_jobs_websocket_streams_changed_snapshot tests/test_routes.py::test_search_grab_and_jobs_pages -v`

  Expected: PASS.

- [ ] **Step 3: Perform the direct browser verification**

  1. Start the app with a real active job and visit `/jobs?tab=active`.
  2. In browser DevTools, confirm one `/ws/jobs/active` connection appears in Network.
  3. Observe progress change without a document navigation.
  4. Transition the job to `organized`; confirm its row disappears, the Active count changes, the Completed count increases, and the active empty state appears if it was the last row.
  5. Switch to `/jobs?tab=completed`; confirm no active-list WebSocket connects.

### Task 4: Full regression verification

**Files:**
- Verify only

- [ ] **Step 1: Run all tests**

  Run: `uv run pytest -v`

  Expected: PASS with no new failures.

- [ ] **Step 2: Inspect the final change set**

  Run: `git diff --check && git diff -- src/skald/routes/jobs.py src/skald/templates/jobs.html src/skald/static/active_jobs.js src/skald/static/style.css tests/test_routes.py`

  Expected: no whitespace errors; changes are confined to the collection socket, active-list DOM contract, client reconciliation, accessibility utility, and tests.
