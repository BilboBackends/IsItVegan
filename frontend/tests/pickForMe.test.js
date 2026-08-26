import test from "node:test";
import assert from "node:assert/strict";

import { pickForMe, pickReason } from "../src/pickForMe.js";

function restaurant(overrides = {}) {
  return {
    id: 1,
    name: "Test Kitchen",
    vegan_score: 8,
    vegan_options: 5,
    vegan_sides: 2,
    vegan_score_parts: { selection: 3, substance: 1, reputation: 1 },
    primary_type: "restaurant",
    open_now: 1,
    ...overrides,
  };
}

test("never recommends a place with nothing vegan", () => {
  const rows = [
    restaurant({ id: 1, vegan_options: 0, vegan_sides: 0 }),
    restaurant({ id: 2, vegan_options: 3 }),
  ];
  for (let i = 0; i < 50; i += 1) {
    assert.equal(pickForMe(rows).id, 2);
  }
});

test("never recommends a low-scoring, archived, hidden or closed-down place", () => {
  const good = restaurant({ id: 9 });
  const rows = [
    good,
    restaurant({ id: 2, vegan_score: 2 }),
    restaurant({ id: 3, archived: 1 }),
    restaurant({ id: 4, consumer_hidden: 1 }),
    restaurant({ id: 5, business_status: "CLOSED_PERMANENTLY" }),
  ];
  for (let i = 0; i < 50; i += 1) {
    assert.equal(pickForMe(rows).id, 9);
  }
});

test("returns null rather than throwing on empty or missing input", () => {
  assert.equal(pickForMe([]), null);
  assert.equal(pickForMe(null), null);
  assert.equal(pickForMe(undefined), null);
  assert.equal(pickForMe([restaurant({ vegan_options: 0, vegan_sides: 0 })]), null);
});

test("openNow filter excludes closed restaurants", () => {
  const rows = [
    restaurant({ id: 1, open_now: 0 }),
    restaurant({ id: 2, open_now: 1 }),
  ];
  for (let i = 0; i < 30; i += 1) {
    assert.equal(pickForMe(rows, { openNow: true }).id, 2);
  }
  assert.equal(pickForMe([restaurant({ open_now: 0 })], { openNow: true }), null);
});

test("maxPrice caps the tier but keeps places Google never priced", () => {
  const rows = [
    restaurant({ id: 1, price_level: "PRICE_LEVEL_VERY_EXPENSIVE" }),
    restaurant({ id: 2, price_level: null }),
  ];
  for (let i = 0; i < 30; i += 1) {
    assert.equal(pickForMe(rows, { maxPrice: 1 }).id, 2);
  }
});

test("excludeId keeps the same place from coming up twice in a row", () => {
  const rows = [restaurant({ id: 1 }), restaurant({ id: 2 })];
  for (let i = 0; i < 30; i += 1) {
    assert.equal(pickForMe(rows, { excludeId: 1 }).id, 2);
  }
  // The only candidate being excluded yields nothing rather than repeating it.
  assert.equal(pickForMe([restaurant({ id: 1 })], { excludeId: 1 }), null);
});

test("samples rather than always returning the single best row", () => {
  const rows = Array.from({ length: 10 }, (_, i) =>
    restaurant({ id: i + 1, vegan_score: 9 - i * 0.1 })
  );
  const seen = new Set();
  for (let i = 0; i < 200; i += 1) seen.add(pickForMe(rows).id);
  assert.ok(seen.size > 1, "a recommender that always returns the top row is just the sort order");
});

test("reason cites a real count and adapts to the venue", () => {
  const reason = pickReason(restaurant({ vegan_options: 5, vegan_sides: 4 }));
  assert.match(reason, /\d/);
  assert.match(reason, /vegan/);

  // Dessert venues count treats, matching the score badge's wording.
  const treats = pickReason(
    restaurant({ primary_type: "ice_cream_shop", vegan_options: 30 })
  );
  assert.match(treats, /treats/);
  assert.doesNotMatch(treats, /meals/);
});

test("reason never names an unappetising raw Google type", () => {
  // "chicken_restaurant" must not surface as "this chicken spot".
  for (let i = 0; i < 40; i += 1) {
    const reason = pickReason(
      restaurant({ primary_type: "chicken_restaurant", vegan_options: 6 })
    );
    assert.doesNotMatch(reason, /chicken/i);
  }
});

test("reason works for a sides-only restaurant", () => {
  const reason = pickReason(
    restaurant({ vegan_options: 0, vegan_sides: 3, vegan_score_parts: {} })
  );
  assert.match(reason, /3 vegan sides/);
});
