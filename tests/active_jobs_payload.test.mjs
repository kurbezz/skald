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
    attention_count: 2, history_count: 7,
  };

  assert.deepEqual(normalizeActiveJobsSnapshot(payload), payload);
});

test("normalizes a deleting active-job snapshot", () => {
  const payload = {
    jobs: [{ id: 42, type: "movie", title: "Example Movie", season: null, episode: null, episode_set: null, status: "deleting", progress: 1 }],
    attention_count: 2, history_count: 7,
  };

  assert.deepEqual(normalizeActiveJobsSnapshot(payload), payload);
});

test("skips a malformed job but keeps valid ones", () => {
  const warn = console.warn;
  console.warn = () => {};
  try {
    const good = { id: 1, type: "movie", title: "Good", season: null, episode: null, episode_set: null, status: "downloading", progress: 0.5 };
    const result = normalizeActiveJobsSnapshot({
      jobs: [{ id: 42, type: "movie", title: "Bad", status: "failed", progress: 0.61 }, good, { ...good }],
      attention_count: 2, history_count: 7,
    });
    assert.deepEqual(result, { jobs: [good], attention_count: 2, history_count: 7 });
  } finally {
    console.warn = warn;
  }
});

test("skips fractional IDs and unsupported media types", () => {
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

  const warn = console.warn;
  console.warn = () => {};
  try {
    assert.deepEqual(
      normalizeActiveJobsSnapshot({ jobs: [{ ...job, id: 42.5 }], attention_count: 0, history_count: 0 }),
      { jobs: [], attention_count: 0, history_count: 0 }
    );
    assert.deepEqual(
      normalizeActiveJobsSnapshot({ jobs: [{ ...job, type: "music" }], attention_count: 0, history_count: 0 }),
      { jobs: [], attention_count: 0, history_count: 0 }
    );
  } finally {
    console.warn = warn;
  }
});

test("preserves TV season and episode fields", () => {
  const payload = {
    jobs: [{ id: 42, type: "tv", title: "Example Show", season: 1, episode: 1, episode_set: "[1,2,3]", status: "downloading", progress: 0.61 }],
    attention_count: 0, history_count: 0,
  };

  assert.deepEqual(normalizeActiveJobsSnapshot(payload), payload);
});

test("rejects malformed root payloads", () => {
  assert.equal(normalizeActiveJobsSnapshot(null), null);
  assert.equal(normalizeActiveJobsSnapshot({ jobs: {}, attention_count: 0, history_count: 0 }), null);
  assert.equal(normalizeActiveJobsSnapshot({ jobs: [], attention_count: -1, history_count: 0 }), null);
});

test("rejects needs_attention and failed jobs in the queue list", () => {
  const warn = console.warn;
  console.warn = () => {};
  try {
    for (const status of ["needs_attention", "failed", "organized"]) {
      const payload = {
        jobs: [{ id: 1, type: "movie", title: "M", season: null, episode: null, episode_set: null, status, progress: 1 }],
        attention_count: 1,
        history_count: 0,
      };
      assert.deepEqual(normalizeActiveJobsSnapshot(payload).jobs, []);
    }
  } finally {
    console.warn = warn;
  }
});

test("rejects snapshots without valid attention/history counts", () => {
  assert.equal(normalizeActiveJobsSnapshot({ jobs: [], attention_count: 0 }), null);
  assert.equal(normalizeActiveJobsSnapshot({ jobs: [], attention_count: 0, history_count: 1.5 }), null);
});
