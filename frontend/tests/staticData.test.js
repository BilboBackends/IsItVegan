import test from "node:test";
import assert from "node:assert/strict";

import {
  appendDataVersion,
  staticAssetUrl,
} from "../src/staticAssetUrls.js";

test("maps new dish bundles to v2 without reusing the legacy gzip URL", () => {
  assert.equal(
    staticAssetUrl("/api/dishes.gz", "/"),
    "/data/dishes-v2.json.gz"
  );
  assert.equal(
    staticAssetUrl("/api/restaurants", "/app/"),
    "/app/data/restaurants.json"
  );
});

test("adds one shared encoded generation to snapshot URLs", () => {
  assert.equal(
    appendDataVersion("/data/restaurants.json", "2026-07-14T12:30:00+00:00"),
    "/data/restaurants.json?v=2026-07-14T12%3A30%3A00%2B00%3A00"
  );
  assert.equal(
    appendDataVersion("/data/menu.json?raw=1", "v2"),
    "/data/menu.json?raw=1&v=v2"
  );
});
