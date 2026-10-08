import test from "node:test";
import assert from "node:assert/strict";

import { backoffDelay, createLiveSocket } from "../src/skald/static/live_socket.mjs";
import { deleteConfirmText } from "../src/skald/static/job_confirm.mjs";

test("backoff is bounded exponential 1s..30s", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map(backoffDelay), [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
});

test("reconnects after close with backoff, and stays closed when not reconnecting", () => {
  const sockets = [];
  class FakeWS {
    constructor() { this.l = {}; this.readyState = 0; sockets.push(this); }
    addEventListener(t, f) { this.l[t] = f; }
    close() {}
  }
  const timers = [];
  const states = [];
  let reconnect = true;
  createLiveSocket({
    url: "ws://x", onMessage() {}, onState: (s) => states.push(s), hooks: false,
    shouldReconnect: () => reconnect, WebSocketImpl: FakeWS,
    setTimer: (fn, ms) => { timers.push(ms); setImmediate(fn); return timers.length; },
    clearTimer() {},
  });
  sockets[0].l.open();
  sockets[0].l.close();
  assert.equal(states.at(-1), "reconnecting");
  assert.deepEqual(timers, [1000]);
  return new Promise((resolve) => setImmediate(() => {
    assert.equal(sockets.length, 2);
    reconnect = false;
    sockets[1].l.close();
    assert.equal(states.at(-1), "closed");
    resolve();
  }));
});

test("delete confirmation names the title and library files", () => {
  assert.match(deleteConfirmText("Film", "organized"), /Film.*organized library files/);
  assert.doesNotMatch(deleteConfirmText("Film", "downloading"), /library/);
});
