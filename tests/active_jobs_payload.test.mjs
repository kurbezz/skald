import test from "node:test";
import assert from "node:assert/strict";

import { normalizeActiveJobsSnapshot } from "../src/skald/static/active_jobs_payload.mjs";

test("normalizes a valid active-jobs snapshot", () => {
  const payload = {
    jobs: [
      {
        id: 42,
        type: "movie",
        title: "Example Movie",
        status: "downloading",
        progress: 0.61,
      },
    ],
    completed_count: 7,
  };

  assert.deepEqual(normalizeActiveJobsSnapshot(payload), payload);
});

test("normalizes a deleting active-job snapshot", () => {
  const payload = {
    jobs: [{ id: 42, type: "movie", title: "Example Movie", status: "deleting", progress: 1 }],
    completed_count: 7,
  };

  assert.deepEqual(normalizeActiveJobsSnapshot(payload), payload);
});

test("rejects a snapshot containing a malformed job", () => {
  assert.equal(
    normalizeActiveJobsSnapshot({
      jobs: [{ id: 42, type: "movie", title: "Example Movie", status: "failed", progress: 0.61 }],
      completed_count: 7,
    }),
    null
  );
});

test("rejects fractional IDs and unsupported media types", () => {
  const job = {
    id: 42,
    type: "movie",
    title: "Example Movie",
    status: "downloading",
    progress: 0.61,
  };

  assert.equal(
    normalizeActiveJobsSnapshot({ jobs: [{ ...job, id: 42.5 }], completed_count: 0 }),
    null
  );
  assert.equal(
    normalizeActiveJobsSnapshot({ jobs: [{ ...job, type: "music" }], completed_count: 0 }),
    null
  );
});

test("rejects malformed root payloads", () => {
  assert.equal(normalizeActiveJobsSnapshot(null), null);
  assert.equal(normalizeActiveJobsSnapshot({ jobs: {}, completed_count: 0 }), null);
  assert.equal(normalizeActiveJobsSnapshot({ jobs: [], completed_count: -1 }), null);
});
