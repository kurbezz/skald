# TV-pack Post-ship Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete all deferred lifecycle hygiene work while preserving token fences, identity-only cleanup, and scalar/pack separation.

**Architecture:** Work in four gated phases: remove obsolete behavior; complete filesystem ownership recovery; reconcile schema and add offline orphan repair before FK enforcement; then make routes/mode transitions atomic. Every phase follows RED→GREEN and ends with focused plus full validation.

**Tech Stack:** Python, SQLModel/SQLAlchemy, SQLite, FastAPI, pytest, Node test runner.

---

## File map

- `src/skald/organizer.py` — obsolete linker; staging/publication cleanup outcomes.
- `src/skald/lifecycle.py` — path identity inspection.
- `src/skald/worker.py` — pack organization/deletion/recovery.
- `src/skald/db.py` — DDL/migration and SQLite connections.
- `src/skald/migrate.py` — new orphan audit/purge module CLI.
- `src/skald/routes/jobs.py` — deletion/retry routes.
- `tests/test_organizer.py`, `tests/test_worker.py`, `tests/test_models.py`, `tests/test_routes.py` — phase regressions.

## Phase A — obsolete code and test truthfulness

### Task 1: Remove the path-only pack linker

**Files:**
- Modify: `src/skald/organizer.py:153-179`
- Modify: `src/skald/worker.py` imports
- Modify: `tests/test_organizer.py:138-238`

- [ ] **Step 1: Confirm all references**

Run `rg -n "link_tv_pack" src tests`. Expected: definition, worker import, and four `test_link_tv_pack_*` tests only.

- [ ] **Step 2: Remove only the superseded API**

Delete the `link_tv_pack(mappings: list[tuple[Path, Path]]) -> list[Path]` definition, its worker import, and the four matching tests. Do not replace it: `stage_file`, `publish_staged_file`, and `organize_tv_pack` remain the exclusive pack pipeline.

- [ ] **Step 3: Verify**

Run `rg -n "link_tv_pack" src tests || true` and `uv run pytest -q tests/test_organizer.py`. Expected: no references and a passing organizer suite.

### Task 2: Make scalar deletion test a real ledger test

**Files:**
- Modify: `tests/test_worker.py:test_scalar_delete_does_not_use_pack_ledger_even_if_rows_exist`

- [ ] **Step 1: Replace the vacuous fixture**

Create distinct scalar and pack files, then persist a scalar job and a real ledger row:

```python
ledger = OrganizedFile(
    job_id=job.id,
    path=str(pack_path),
    operation_token="stale-pack-token",
    lifecycle=OrganizedFileLifecycle.PUBLISHED,
    published_device=pack_path.stat().st_dev,
    published_inode=pack_path.stat().st_ino,
)
session.add_all([job, ledger])
session.commit()
```

- [ ] **Step 2: Assert scalar isolation**

After `request_job_deletion` and `reconcile_deleting_job`, assert:

```python
assert not scalar_path.exists()
assert pack_path.exists()
assert session.get(OrganizedFile, ledger.id) is not None
assert fake_qbit.deleted_hashes == [job.hash]
```

Run `uv run pytest -q tests/test_worker.py::test_scalar_delete_does_not_use_pack_ledger_even_if_rows_exist`. Expected: PASS, or a focused failure to resolve in Task 8.

## Phase B — staging ownership and filesystem hygiene

### Task 3: Persist and clean owned staging artifacts

**Files:**
- Modify: `src/skald/organizer.py:195-329,457-479`
- Modify: `src/skald/worker.py:209-327,500-640,675-879`
- Test: `tests/test_organizer.py:243-359`
- Test: `tests/test_worker.py:916-1013,1466-1660`

- [ ] **Step 1: Write a failing foreign-race cleanup test**

Arrange a public `row.path` replaced by a foreign inode plus an owned retained `staging_path`. Reconcile deletion and assert:

```python
assert outcome is DeletionOutcome.NEEDS_ATTENTION
assert foreign_public_path.exists()
assert not owned_staging_path.exists()
assert str(foreign_public_path) in refreshed_job.error_message
assert str(owned_staging_path) in refreshed_job.error_message
assert session.get(OrganizedFile, row.id) is not None
```

Run `uv run pytest -q tests/test_worker.py -k "identity_mismatch or foreign"`. Expected: FAIL because conflict returns before staging cleanup.

- [ ] **Step 2: Clean all identity-qualified candidates before classification**

In both reconciliation functions, collect the public candidate from `_public_identity_for_cleanup(row)` and the private candidate from `row.staging_path` with its recorded staging identity. Call `cleanup_owned_file` for each before classifying clean, foreign, or transient error. Keep foreign files/ledger state and use:

```python
detail = (
    f"ownership conflict while cleaning {row.path}; "
    f"staging path {row.staging_path} was also checked"
)
```

- [ ] **Step 3: Write the failed-stage durability test**

Force `stage_file` to fail after creating a private file. Verify the reservation row contains each `exc.outcome.attempt_owned_identities` device/inode for its `staging_path`; invoke organizing recovery and verify only the matching file is removed. Run `uv run pytest -q tests/test_worker.py -k "staging and failure"`. Expected: FAIL because only error text persists.

- [ ] **Step 4: Persist ownership with the current job/token fence**

In `organize_tv_pack`'s `FileOperationError` handling, correlate every owned identity to its reservation by `staging_path`, then conditionally update only its current organizing row:

```python
values = {
    "staging_device": identity.device,
    "staging_inode": identity.inode,
}
```

Use the same job/token/lifecycle predicate as the existing row transitions. On zero rows, rollback and let the current owner recover; never record stale ownership.

- [ ] **Step 5: Remove only an empty token directory**

Add beside `prune_empty_parent`:

```python
def prune_empty_staging_directory(path: Path) -> None:
    directory = path.parent
    if directory.parent.name != ".skald-staging":
        return
    try:
        directory.rmdir()
    except OSError:
        return
```

Call it only after identity-verified cleanup. Test that the token dir disappears but `.skald-staging` and sibling token dirs remain. Then run `uv run pytest -q tests/test_worker.py -k "identity_mismatch or staging"`.

### Task 4: Normalize filesystem outcomes

**Files:**
- Modify: `src/skald/lifecycle.py:12-26`
- Modify: `src/skald/organizer.py:195-399`
- Test: `tests/test_organizer.py:243-359`

- [ ] **Step 1: Add RED tests**

Cover dangling symlink, permission error, new staging parent, pre-existing staging path, copy fallback `EEXIST`, publish-parent fsync failure, and cleanup failure. Each assertion must show a non-clean structured outcome with attempted path and `errno` for an `OSError`.

- [ ] **Step 2: Use non-following identity collection**

Implement:

```python
def file_identity(path: Path) -> FileIdentity:
    result = path.lstat()
    return FileIdentity(device=result.st_dev, inode=result.st_ino)
```

`identity_matches` returns `False` only for `FileNotFoundError`; other `OSError`s reach `cleanup_owned_file` as `error` rather than silent absence.

- [ ] **Step 3: Preserve the primary error and cleanup detail**

For every `mkdir`, `link`, `copy`, `fsync`, `unlink`, or identity failure, raise:

```python
raise FileOperationError(
    exc,
    FileOperationOutcome(
        attempt_owned_paths=owned_paths,
        attempt_owned_identities=owned_identities,
        explicitly_unowned_paths=unowned_paths,
        cleanup_errors=cleanup_errors,
    ),
) from exc
```

Fsync newly created staging directories. On publication-parent fsync failure, call `cleanup_owned_file(target, staged.identity)` and append its cleanup error instead of masking the fsync error.

- [ ] **Step 4: Verify Phase B**

Run `uv run pytest -q tests/test_organizer.py`, `uv run pytest -q tests/test_worker.py -k "staging or identity_mismatch"`, and `git diff --check`. Expected: all pass/no diff-check output.

## Phase C — schema and foreign-key integrity

### Task 5: Align DDL and enable FKs for application engines

**Files:**
- Modify: `src/skald/db.py:4-80`
- Modify: `src/skald/models.py:66-76`
- Test: `tests/test_models.py:116-208`

- [ ] **Step 1: Write schema-parity/FK RED tests**

On a fresh `create_all` DB, assert `PRAGMA foreign_key_list(organizedfile)` includes `job_id → mediajob.id`; with enforcement enabled, assert an orphan insert raises `IntegrityError`. Build an old DB, run `migrate_schema`, and assert matching constraints/indexes/nullable identity columns plus uppercase `SCALAR`, `PACK`, and `LEGACY_UNVERIFIED` defaults.

- [ ] **Step 2: Make FK enforcement selectable only for the offline repair tool**

Change the factory to:

```python
def get_engine(db_path: str, *, enforce_foreign_keys: bool = True) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}")
    if enforce_foreign_keys:
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
    return engine
```

All app callers retain the default; Task 6's tool uses `False` while repairing legacy records.

- [ ] **Step 3: Make migration DDL model-equivalent**

Use the same `job_id INTEGER NOT NULL REFERENCES mediajob(id)`, unique `path`, nullable identity fields, indexes, and uppercase enum-name defaults in `migrate_schema` as in `OrganizedFile`. Do not alter existing `mediajob.status` or `type` storage encoding.

- [ ] **Step 4: Verify**

Run `uv run pytest -q tests/test_models.py tests/test_worker.py -k "schema or migration or legacy"`. Expected: PASS.

### Task 6: Add offline orphan export/purge utility

**Files:**
- Create: `src/skald/migrate.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write JSON/CSV RED tests**

With an engine created as `get_engine(path, enforce_foreign_keys=False)`, create one valid and one orphan ledger row. Verify JSON and CSV audit contents, exact purge count, valid-row survival, and rollback when audit writing raises.

- [ ] **Step 2: Implement one-transaction export then purge**

Implement `export_and_purge_orphans(engine, audit_path, *, audit_format)` with `engine.begin()`: select `organizedfile` rows missing a `mediajob` via `LEFT JOIN`, write a list-of-dicts JSON or header-bearing CSV, then delete exactly selected IDs using an expanding SQLAlchemy parameter. Return count; raise `ValueError` except for `json`/`csv`; never toggle the pragma. An audit-write exception must escape and roll back deletion.

- [ ] **Step 3: Add a no-dependency module CLI**

Use `argparse` positional `database`, positional `audit_path`, required `--format` choices `json,csv`. Call `get_engine(database, enforce_foreign_keys=False)`, invoke the utility, and print `exported and purged {count} orphan organizedfile rows`. Support `uv run python -m skald.migrate --help`.

- [ ] **Step 4: Verify Phase C**

Run `uv run pytest -q tests/test_models.py` and `uv run python -m skald.migrate --help`. Expected: tests pass and CLI help shows two positionals plus `--format`.

## Phase D — route contention and scalar transitions

### Task 7: Return HTTP 409 on lock contention

**Files:**
- Modify: `src/skald/routes/jobs.py:208-237,331-375`
- Test: `tests/test_routes.py:1285-1330`

- [ ] **Step 1: Write delete/retry RED tests**

Hold a target job lock and request each route. Assert:

```python
assert response.status_code == 409
assert "already being updated" in response.text
assert refreshed.status is original_status
assert refreshed.operation_token == original_token
assert fake_qbit.deleted_hashes == []
```

For retry, assert all ledger rows are unchanged.

- [ ] **Step 2: Return before all side effects**

When `try_job_lock(job_id)` yields false, return:

```python
return HTMLResponse(
    "This job is already being updated; retry shortly.", status_code=409
)
```

Place the branch before request/deletion calls, session mutation, qBittorrent, or filesystem work; preserve existing successful redirects.

- [ ] **Step 3: Verify**

Run `uv run pytest -q tests/test_routes.py -k "contention"`. Expected: PASS.

### Task 8: Clear nonlegacy pack residue atomically on scalar retry

**Files:**
- Modify: `src/skald/routes/jobs.py:331-375`
- Test: `tests/test_routes.py`
- Test: `tests/test_worker.py:test_scalar_delete_does_not_use_pack_ledger_even_if_rows_exist`

- [ ] **Step 1: Write the mode-transition RED test**

Arrange a pack job with token, normal ledger row, and `LEGACY_UNVERIFIED` row. After scalar retry assert:

```python
assert refreshed.organization_mode is OrganizationMode.SCALAR
assert refreshed.operation_token is None
assert session.get(OrganizedFile, normal_row.id) is None
assert session.get(OrganizedFile, legacy_row.id) is not None
```

Inject a flush/delete failure and assert original mode, token, and both rows survive rollback.

- [ ] **Step 2: Implement one locked transaction**

Retain `DELETING` rejection. Within the already-acquired lock, delete only this job's nonlegacy rows and clear token before the existing single commit:

```python
session.exec(
    delete(OrganizedFile).where(
        OrganizedFile.job_id == job.id,
        OrganizedFile.lifecycle != OrganizedFileLifecycle.LEGACY_UNVERIFIED,
    )
)
job.operation_token = None
job.organization_mode = OrganizationMode.SCALAR
```

Rollback any exception so no partial mode change persists.

- [ ] **Step 3: Verify Phase D**

Run `uv run pytest -q tests/test_routes.py tests/test_worker.py -k "scalar or retry"`. Expected: PASS.

## Gates and final evidence

### Task 9: Validate every phase before continuing

**Files:**
- Modify: `.slim/deepwork/tv-pack-postship-hygiene.md`

- [ ] **Step 1: Record a gate after every phase**

Record changed paths, focused test output, full-suite output, and unresolved risk. Request `@oracle` review of obsolete removal (A), identity/recovery safety (B), migration/data integrity (C), and lock/mode atomicity (D).

- [ ] **Step 2: Run final checks once all gates pass**

Run `uv run pytest -q`, `node --test tests/active_jobs_payload.test.mjs`, and `git diff --check`. Expected: all tests pass and the diff check has no output.

- [ ] **Step 3: Preserve commit control**

Present validation evidence and do not commit, amend, push, or create a PR unless explicitly requested.
