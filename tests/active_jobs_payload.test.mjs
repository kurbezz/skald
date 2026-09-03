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
        season: null,
        episode: null,
        episode_set: null,
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
    jobs: [{ id: 42, type: "movie", title: "Example Movie", season: null, episode: null, episode_set: null, status: "deleting", progress: 1 }],
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
    season: null,
    episode: null,
    episode_set: null,
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

test("preserves TV season and episode fields", () => {
  const payload = {
    jobs: [{ id: 42, type: "tv", title: "Example Show", season: 1, episode: 1, episode_set: "[1,2,3]", status: "downloading", progress: 0.61 }],
    completed_count: 0,
  };

  assert.deepEqual(normalizeActiveJobsSnapshot(payload), payload);
});

test("rejects malformed root payloads", () => {
  assert.equal(normalizeActiveJobsSnapshot(null), null);
  assert.equal(normalizeActiveJobsSnapshot({ jobs: {}, completed_count: 0 }), null);
  assert.equal(normalizeActiveJobsSnapshot({ jobs: [], completed_count: -1 }), null);
});
