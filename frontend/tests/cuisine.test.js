import test from "node:test";
import assert from "node:assert/strict";

import {
  isDessertVenue,
  preferredMenuCategory,
  venueKind,
  venueKindLabel,
} from "../src/cuisine.js";

test("opens clear dessert businesses on a nonempty Desserts tab", () => {
  assert.equal(
    preferredMenuCategory("ice_cream_shop", {
      food: 13,
      dessert: 23,
      drink: 0,
    }),
    "dessert"
  );
  assert.equal(
    preferredMenuCategory("dessert_shop", {
      food: 26,
      dessert: 13,
      drink: 82,
    }),
    "dessert"
  );
  assert.equal(
    preferredMenuCategory("ice_cream_shop", {
      food: 4,
      dessert: 0,
      drink: 2,
    }),
    "food"
  );
});

test("uses menu balance for bakeries and keeps bagel shops food-first", () => {
  assert.equal(
    preferredMenuCategory("bakery", { food: 12, dessert: 53 }),
    "dessert"
  );
  assert.equal(
    preferredMenuCategory("bakery", { food: 94, dessert: 29 }),
    "food"
  );
  assert.equal(
    preferredMenuCategory("bagel_shop", { food: 93, dessert: 4 }),
    "food"
  );
});

test("falls back to the first meaningful nonempty category", () => {
  assert.equal(preferredMenuCategory("restaurant", { dessert: 4 }), "dessert");
  assert.equal(preferredMenuCategory("coffee_shop", { drink: 12 }), "drink");
  assert.equal(preferredMenuCategory("restaurant", {}), "food");
});

test("groups map venues without name-based guessing", () => {
  assert.equal(isDessertVenue("cake_shop"), true);
  assert.equal(isDessertVenue("pastry_shop"), true);
  assert.equal(venueKind("cake_shop"), "dessert");
  assert.equal(venueKind("bakery"), "dessert");
  assert.equal(venueKind("bagel_shop"), "restaurant");
  assert.equal(venueKind("coffee_shop"), "coffee");
  assert.equal(venueKind("cafe"), "coffee");
  assert.equal(venueKind("tea_house"), "coffee");
  assert.equal(venueKind("juice_shop"), "restaurant");
  assert.equal(venueKindLabel("dessert"), "Dessert / treats");
});
