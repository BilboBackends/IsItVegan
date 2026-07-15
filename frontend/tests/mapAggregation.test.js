import test from "node:test";
import assert from "node:assert/strict";

import {
  aggregateMapItems,
  mapItemsForViewport,
  withPriorityMapItem,
} from "../src/mapAggregation.js";

const items = [
  { id: 1, lat: 28.54, lng: -81.38 },
  { id: 2, lat: 28.541, lng: -81.381 },
  { id: 3, lat: 26.64, lng: -81.94 },
];

test("groups nearby map items while zoomed out", () => {
  const entries = aggregateMapItems(items, 10);
  assert.equal(entries.length, 2);
  assert.equal(entries.find((entry) => entry.cluster)?.items.length, 2);
});

test("keeps a selected map item outside its low-zoom cluster", () => {
  const entries = aggregateMapItems(items, 10, (item) => item.id, 2);
  const selected = entries.find((entry) => entry.key === "item:2");
  assert.deepEqual(selected?.items.map((item) => item.id), [2]);
  assert.equal(selected?.cluster, false);
  assert.ok(
    entries
      .filter((entry) => entry.cluster)
      .every((entry) => entry.items.every((item) => item.id !== 2))
  );
});

test("keeps individual map items at neighborhood zoom", () => {
  const entries = aggregateMapItems(items, 13);
  assert.equal(entries.length, 3);
  assert.ok(entries.every((entry) => !entry.cluster));
});

test("keeps all items for low-zoom clustering", () => {
  assert.equal(
    mapItemsForViewport(items, 12, {
      s: 28.5,
      w: -81.4,
      n: 28.6,
      e: -81.3,
    }).length,
    3
  );
});

test("limits individual pins to padded high-zoom bounds", () => {
  const visible = mapItemsForViewport(items, 13, {
    s: 28.535,
    w: -81.39,
    n: 28.545,
    e: -81.37,
  });
  assert.deepEqual(visible.map((item) => item.id), [1, 2]);
});

test("adds one selected item back after high-zoom viewport culling", () => {
  const visible = mapItemsForViewport(items, 13, {
    s: 28.535,
    w: -81.39,
    n: 28.545,
    e: -81.37,
  });
  const prioritized = withPriorityMapItem(visible, items[2]);
  assert.deepEqual(prioritized.map((item) => item.id), [1, 2, 3]);
  assert.strictEqual(withPriorityMapItem(prioritized, items[2]), prioritized);
});
