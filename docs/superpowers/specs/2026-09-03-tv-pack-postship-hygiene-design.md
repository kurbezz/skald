# TV-pack post-ship hygiene design

## Goal

Complete the deferred TV-pack lifecycle hygiene backlog without weakening the
shipped token-fenced, identity-aware organization and deletion model.

## Constraints

- A pack file is deleted only after its durable identity is verified.
- `legacy_unverified` ledger rows remain outside automatic cleanup.
- Lifecycle token fencing remains authoritative over in-memory state.
- Migration must preserve an auditable record before deleting orphan ledger
  rows, then enable SQLite foreign-key enforcement.
- New behavior is developed test-first and delivered in independently
  verifiable phases.

## Delivery phases

### A. Remove obsolete behavior and repair test intent

Delete the superseded path-only `link_tv_pack` API, its imports, and its four
tests. Strengthen the scalar-deletion regression so it creates real
`OrganizedFile` rows and proves scalar deletion ignores the pack ledger and
uses only its scalar library path.

### B. Ownership, staging, and filesystem hygiene

On a foreign-identity conflict during pack deletion, clean this attempt's
identity-owned staging artifact when safe and report both the public and
staging paths in the operator-facing error. Persist owned `(path, device,
inode)` data from staging failures so reconciliation can safely remove only
attempt-owned leftovers. Remove empty `.skald-staging/<token>` directories
after their owned contents are cleaned.

Harden Phase 2 hygiene paths: use `lstat` for identity collection; make
permission and dangling-symlink cleanup outcomes observable; return a defined
structured outcome for filesystem `OSError`s; fsync newly-created staging
parents; and add regressions for publish-fsync rollback, pre-existing staging,
copy `EEXIST`, and propagated cleanup errors.

### C. Ledger schema and referential integrity

Make the `OrganizedFile` DDL used by fresh `create_all` and the additive legacy
migration agree on constraints, enum-name defaults, and indexes. Keep the
uppercase Enum-name encoding already required by SQLModel.

Provide an explicit migration utility that, before foreign keys are enabled,
exports each orphan `organizedfile` record into a user-selected JSON or CSV
audit file and deletes those exact rows in one transaction. The utility reports
counts and refuses to enable foreign keys itself. Application startup then
enables `PRAGMA foreign_keys=ON` for every SQLite connection. Tests cover fresh
schema, legacy migration/read-back, export-and-purge behavior, and FK rejection
of new orphan rows.

### D. Route and mode-transition semantics

Delete and retry routes return a distinct contention response (409, with a
retryable message) when the per-job advisory lock is already held; no durable
state or external side effect occurs. On pack-to-scalar retry, clear the stale
operation token and remove residual non-legacy pack ledger rows atomically with
the scalar transition. Scalar cleanup continues not to consume pack-ledger
identities.

## Error handling

Identity mismatches are ownership conflicts: preserve foreign files, retain
durable recovery state, and produce complete operator detail. Transient
filesystem failures preserve retry state and expose structured errno/path
information. Migration export/write failure rolls back the corresponding purge.

## Verification

Each phase adds focused RED→GREEN regression tests, then runs its focused suite.
Every phase ends with `uv run pytest -q`, `node --test
tests/active_jobs_payload.test.mjs`, and `git diff --check`. A lifecycle-risk
review gate follows each phase before the next begins.
