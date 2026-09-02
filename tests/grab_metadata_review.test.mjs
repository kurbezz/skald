import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { initializeGrabMetadataReview } from "../src/skald/static/grab_metadata_review.mjs";

test("hides collapsed grab review fields", async () => {
  const stylesheet = await readFile(new URL("../src/skald/static/style.css", import.meta.url), "utf8");

  assert.match(
    stylesheet,
    /\.grab-review-fields\[hidden\]\s*\{[^}]*\bdisplay\s*:\s*none\s*;/,
  );
});

test("opens only the selected review region and focuses its title input", () => {
  const createReview = () => {
    const title = { focused: false, focus() { this.focused = true; } };
    const fields = {
      hidden: true,
      querySelector(selector) { return selector === '[name="title"]' ? title : null; },
    };
    const review = {
      querySelector(selector) { return selector === "[data-grab-review-fields]" ? fields : null; },
    };
    const button = {
      attributes: { "aria-expanded": "false" },
      addEventListener(_event, listener) { this.listener = listener; },
      getAttribute(name) { return this.attributes[name]; },
      setAttribute(name, value) { this.attributes[name] = value; },
      closest(selector) { return selector === "[data-grab-review]" ? review : null; },
    };

    return { button, fields, title };
  };
  const first = createReview();
  const second = createReview();
  const root = {
    querySelectorAll(selector) {
      return selector === "[data-grab-review-toggle]" ? [first.button, second.button] : [];
    },
  };

  initializeGrabMetadataReview(root);
  first.button.listener();

  assert.equal(first.button.getAttribute("aria-expanded"), "true");
  assert.equal(first.fields.hidden, false);
  assert.equal(first.title.focused, true);
  assert.equal(second.button.getAttribute("aria-expanded"), "false");
  assert.equal(second.fields.hidden, true);
  assert.equal(second.title.focused, false);
});
