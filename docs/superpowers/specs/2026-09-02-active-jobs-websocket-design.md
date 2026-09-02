# Active jobs WebSocket updates

## Goal

Keep `/jobs?tab=active` current without a page reload. The page opens one
same-origin WebSocket and reconciles its rows, status badges, progress values,
tab counts, and empty state in place.

## Transport

- Add an authenticated `/ws/jobs/active` endpoint **before** the dynamic
  `/ws/jobs/{job_id}` route.
- Poll at the existing two-second WebSocket cadence.
- Send only changed, newest-first authoritative snapshots:

  ```json
  {
    "jobs": [{"id": 42, "type": "movie", "title": "Example", "status": "downloading", "progress": 0.613}],
    "completed_count": 7
  }
  ```

- `jobs` contains only the existing active statuses. Its length is the active
  count; `completed_count` keeps the visible completed-tab badge current.
- Keep the existing detail-page socket unchanged.

## Page contract

- Load a dedicated active-list client script only when `tab == "active"`.
- Preserve the current visual classes and copy while adding stable data hooks:
  a list container, active/completed count badges, table and empty-state
  containers, an ordered row list, and a row keyed by `data-job-id`.
- Rows expose scoped hooks for their status badge/label and progress fill/text.
  The progress cell receives standard `progressbar` ARIA values.
- Render both the table and the existing empty state on the active page, using
  `hidden` to switch between them. Include an inert row template for newly
  observed active jobs and a polite live region for collection changes.

## Client reconciliation

For each valid snapshot, the client:

1. Patches matching rows: replaces the single `badge-{status}` class and label,
   clamps progress to 0–1, then updates the fill, displayed percent, and ARIA
   attributes.
2. Creates rows for new IDs using `textContent`, never HTML interpolation.
3. Reorders rows to match the snapshot and removes absent rows. A job that
   becomes terminal therefore disappears from the active table without reload.
4. Updates both tab counts and toggles the table/empty state when the final row
   is removed.
5. Announces collection additions/removals—not routine progress ticks—and only
   moves focus to the Active tab when it was inside a removed row.

Invalid WebSocket messages, unavailable WebSocket support, or a closed socket
leave the server-rendered page usable with manual refresh; no error banner or
automatic reload is introduced.

## Verification

Before implementation, add a failing server-route test that opens the
active-list socket, asserts its initial snapshot, changes a job to a terminal
status, and asserts the next snapshot removes it. Add template coverage that
the active tab includes the list script/hooks while the completed tab does not.
Add client behavior tests for scoped progress/status patching, row removal,
new-row insertion, counts, empty-state transition, and no reload. Then run the
focused tests and the full test suite.
