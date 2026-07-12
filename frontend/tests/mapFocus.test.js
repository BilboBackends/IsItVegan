import assert from "node:assert/strict";
import test from "node:test";
import { PLACE_FOCUS_ZOOM, placeFocusZoom } from "../src/mapFocus.js";

test("place focus zooms a distant map in to street level", () => {
  assert.equal(placeFocusZoom(10), PLACE_FOCUS_ZOOM);
  assert.equal(placeFocusZoom(15), PLACE_FOCUS_ZOOM);
});

test("place focus preserves a closer zoom level", () => {
  assert.equal(placeFocusZoom(17), 17);
  assert.equal(placeFocusZoom(19), 19);
});

test("place focus has a safe fallback for a missing zoom", () => {
  assert.equal(placeFocusZoom(undefined), PLACE_FOCUS_ZOOM);
});
