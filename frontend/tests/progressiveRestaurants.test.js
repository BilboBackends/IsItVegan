import assert from "node:assert/strict";
import test from "node:test";
import { progressiveRestaurantSections } from "../src/progressiveRestaurants.js";

function restaurants(count, start = 1) {
  return Array.from({ length: count }, (_, index) => ({
    place_id: `place-${start + index}`,
  }));
}

test("keeps a deep selected restaurant visible without rebuilding the full list", () => {
  const onMap = restaurants(540);
  const sections = progressiveRestaurantSections({
    onMap,
    offMap: [],
    filtered: onMap,
    visibleLimit: 60,
    selectedRestaurantId: "place-500",
  });

  assert.equal(sections.visibleOnMap.length, 60);
  assert.equal(sections.visibleOffMap.length, 0);
  assert.equal(sections.pinnedFocused.place_id, "place-500");
});

test("does not duplicate a selected restaurant already inside the page", () => {
  const onMap = restaurants(100);
  const sections = progressiveRestaurantSections({
    onMap,
    offMap: [],
    filtered: onMap,
    visibleLimit: 60,
    selectedRestaurantId: "place-25",
  });

  assert.equal(sections.visibleOnMap.length, 60);
  assert.equal(sections.pinnedFocused, null);
});

test("pins an off-map selection without expanding either section", () => {
  const onMap = restaurants(40);
  const offMap = restaurants(100, 41);
  const filtered = [...onMap, ...offMap];
  const sections = progressiveRestaurantSections({
    onMap,
    offMap,
    filtered,
    visibleLimit: 60,
    selectedRestaurantId: "place-120",
  });

  assert.equal(sections.visibleOnMap.length, 40);
  assert.equal(sections.visibleOffMap.length, 20);
  assert.equal(sections.pinnedFocused.place_id, "place-120");
});
