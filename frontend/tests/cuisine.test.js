import test from "node:test";
import assert from "node:assert/strict";

import {
  cuisineLabel,
  cuisineOptions,
  isDessertVenue,
  preferredMenuCategory,
  venueKind,
  venueKindLabel,
} from "../src/cuisine.js";

test("consolidates cuisine one-offs into broad groups", () => {
  assert.equal(cuisineLabel("burrito_restaurant"), "Mexican & Latin");
  assert.equal(cuisineLabel("cuban_restaurant"), "Mexican & Latin");
  assert.equal(cuisineLabel("turkish_restaurant"), "Mediterranean & Middle Eastern");
  assert.equal(cuisineLabel("steak_house"), "American & Burgers");
  assert.equal(cuisineLabel("chicken_wings_restaurant"), "American & Burgers");
  assert.equal(cuisineLabel("juice_shop"), "Sweets & Treats");
  assert.equal(cuisineLabel("winery"), "Bars & Breweries");
  assert.equal(cuisineLabel("asian_fusion_restaurant"), "Other Asian");
  assert.equal(cuisineLabel("food_court"), "Fast Food");
});

test("sends generic and unknown types to Other, never a one-off label", () => {
  assert.equal(cuisineLabel("restaurant"), "Other");
  assert.equal(cuisineLabel("coworking_space"), "Other");
  assert.equal(cuisineLabel("hotel"), "Other");
  assert.equal(cuisineLabel("meal_takeaway"), "Other");
  assert.equal(cuisineLabel("fine_dining_restaurant"), "Other");
  assert.equal(cuisineLabel(""), "Other");
  assert.equal(cuisineLabel(null), "Other");
});

test("resolves substring collisions to the more specific group", () => {
  // These types contain another group's keyword as a substring; order in
  // GROUPS decides them and must not regress.
  assert.equal(cuisineLabel("latin_american_restaurant"), "Mexican & Latin"); // ⊃ american
  assert.equal(cuisineLabel("pizza_delivery"), "Italian & Pizza"); // ⊃ deli
  assert.equal(cuisineLabel("steak_house"), "American & Burgers"); // ⊃ tea
  assert.equal(cuisineLabel("tea_house"), "Coffee & Tea");
  assert.equal(cuisineLabel("barbecue_restaurant"), "Barbecue & Southern"); // ⊃ bar
  assert.equal(cuisineLabel("oyster_bar_restaurant"), "Seafood"); // ⊃ bar
  assert.equal(cuisineLabel("bar_and_grill"), "Bars & Breweries");
});

test("keeps Other last in the dropdown options", () => {
  const options = cuisineOptions([
    { primary_type: "hotel" },
    { primary_type: "vegan_restaurant" },
    { primary_type: "italian_restaurant" },
  ]);
  assert.deepEqual(options, ["Italian & Pizza", "Vegan & Health", "Other"]);
});

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
