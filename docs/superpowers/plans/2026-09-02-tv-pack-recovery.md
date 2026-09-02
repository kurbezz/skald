# TV Pack Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make TV-pack organization and deletion resumable after filesystem or database failures.

**Architecture:** `OrganizedFile` becomes a write-ahead ledger: a committed row reserves every target before the first filesystem write and remains until the corresponding target is confirmed absent. `ORGANIZING` and new `DELETING` statuses express durable intent; worker and deletion logic reconcile those states idempotently.

**Tech Stack:** Python 3.12, SQLModel, SQLite, FastAPI, pytest.

---

## File structure

- `src/skald/models.py` — adds the `DELETING` job status.
- `src/skald/db.py` — adds uniqueness for the path reservation ledger.
- `src/skald/organizer.py` — reports every path created or partially created in pack operations.
- `src/skald/worker.py` — persists pack plans before writes, finalizes manifests, and reconciles interrupted organization.
- `src/skald/routes/jobs.py` — persists deletion intent then removes library files before qBittorrent content.
- `tests/test_worker.py` — covers prepare/write/finalize/recovery transitions.
- `tests/test_routes.py` — covers resumable deletion failures and scalar-path compatibility.
- `tests/test_organizer.py` — covers ownership and cleanup errors for the current failed copy target.

### Task 1: Write-ahead TV-pack ledger

**Files:**
- Modify: `src/skald/organizer.py`, `src/skald/worker.py`, `tests/test_organizer.py`, `tests/test_worker.py`

- [ ] **Step 1: Write failing recovery tests**

  Add tests proving that before `link_tv_pack` is called the database has an
  `ORGANIZING` job and one `OrganizedFile` row per preflight target; preparation
  commit failure makes no filesystem calls; write failure with successful cleanup
  leaves no rows; cleanup failure retains a row for each residual target; and
  a new worker pass reconciles an interrupted `ORGANIZING` job before retrying.

- [ ] **Step 2: Run RED**

  Run: `uv run pytest tests/test_worker.py tests/test_organizer.py -k 'ledger or organizing_recovery or partial_copy' -v`

  Expected: FAIL because the manifest is written only after filesystem success.

- [ ] **Step 3: Implement prepare/write/finalize transitions**

  1. Preflight mappings with no writes.
  2. In one transaction, insert every `OrganizedFile`, set `ORGANIZING`, clear
     `library_path` and error, then commit.
  3. Call `link_tv_pack`; make it report the current partially-created target
     as attempt-owned and preserve both write and cleanup errors.
  4. On write failure, remove confirmed-absent rows and retain rows for
     residual paths, then commit `NEEDS_ATTENTION`.
  5. On successful links, commit only the `ORGANIZED` finalization while keeping
     the existing rows. If finalization fails, clean targets and use the ledger
     to retain only residual paths under `NEEDS_ATTENTION`.
  6. Add a reconciler for durable `ORGANIZING` jobs that idempotently removes
     ledger paths; it transitions to `COMPLETED` when clear, otherwise retains
     residual rows and transitions to `NEEDS_ATTENTION`.

- [ ] **Step 4: Run GREEN**

  Run: `uv run pytest tests/test_worker.py tests/test_organizer.py -k 'ledger or organizing_recovery or partial_copy or tv_pack' -v`

  Expected: PASS.

### Task 2: Resumable deletion intent

**Files:**
- Modify: `src/skald/models.py`, `src/skald/db.py`, `src/skald/worker.py`, `src/skald/routes/jobs.py`, `tests/test_routes.py`, `tests/test_worker.py`

- [ ] **Step 1: Write failing deletion-state tests**

  Add tests proving a delete request commits `DELETING` before the first
  library or qBittorrent side effect; a second-library-file failure leaves the
  job and all ledger rows in `DELETING` without calling qBittorrent; qBittorrent
  failure after library cleanup retains the job for retry; and a later worker
  pass completes an idempotent deletion when paths and torrent are absent.

- [ ] **Step 2: Run RED**

  Run: `uv run pytest tests/test_routes.py tests/test_worker.py -k 'deleting or delete_pending' -v`

  Expected: FAIL because deletion destroys qBittorrent content before durable
  deletion intent and has no recovery state.

- [ ] **Step 3: Implement the `DELETING` protocol**

  1. Add `DELETING = "deleting"` to `JobStatus`; include it in active-job
     display but exclude it from normal torrent polling.
  2. In `delete_job`, commit `DELETING` before side effects.
  3. Reconcile recorded paths first. On a filesystem failure retain all rows,
     retain `DELETING`, persist a `Delete pending:` message, and return a 500.
  4. Only after every library path is absent call qBittorrent deletion. Preserve
     `DELETING` and the ledger on failure with a 502 response.
  5. After both layers succeed, delete ledger rows and the job in one database
     transaction. Treat absent paths and an already-absent torrent as success.
  6. Have the worker retry `DELETING` reconciliation; never let the generic
     poll error handler convert `ORGANIZING` or `DELETING` into `FAILED`.

- [ ] **Step 4: Run GREEN**

  Run: `uv run pytest tests/test_routes.py tests/test_worker.py -k 'deleting or delete_pending or delete_job' -v`

  Expected: PASS.

### Task 3: Validate the complete recovery model

**Files:**
- Verify only

- [ ] **Step 1: Run the full suite**

  Run: `uv run pytest -q`

  Expected: PASS.

- [ ] **Step 2: Inspect the complete change set**

  Run: `git diff --check && git diff -- src/skald/models.py src/skald/db.py src/skald/organizer.py src/skald/worker.py src/skald/routes/jobs.py tests/test_organizer.py tests/test_worker.py tests/test_routes.py`

  Expected: no whitespace errors and no unrelated changes.
