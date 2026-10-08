import assert from "node:assert/strict";
import test from "node:test";

import { initializeGrabSubmitGuard } from "../src/skald/static/grab_metadata_review.mjs";

test("disables the grab button and shows progress on submit", async () => {
  const button = { disabled: false, textContent: "Grab" };
  const form = {
    addEventListener(_event, listener) { this.listener = listener; },
    querySelector(selector) { return selector === "[data-grab-submit]" ? button : null; },
  };
  const other = { disabled: false, textContent: "Grab" };
  const otherForm = {
    addEventListener(_event, listener) { this.listener = listener; },
    querySelector() { return other; },
  };
  const root = {
    querySelectorAll(selector) {
      return selector === "[data-grab-form]" ? [form, otherForm] : [];
    },
  };

  initializeGrabSubmitGuard(root);
  form.listener();
  await new Promise((resolve) => setTimeout(resolve, 5));

  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Adding…");
  assert.equal(other.disabled, false);
  assert.equal(other.textContent, "Grab");
});
