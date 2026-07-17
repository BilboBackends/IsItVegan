import test from "node:test";
import assert from "node:assert/strict";

import {
  buildDishSearchIndex,
  dishMatchesQuery,
  dishSearchScore,
  parseDishQuery,
  queryIntentLabels,
} from "../src/dishSearch.js";

const baseDish = {
  name: "Garden Bowl",
  raw_description: "seasonal vegetables over grains",
  verdict: "vegan",
  category: "food",
};

function matches(dish, query) {
  const intent = parseDishQuery(query);
  return dishMatchesQuery(dish, intent, buildDishSearchIndex(dish));
}

test("dish_format matches format-word queries the menu text never says", () => {
  const ramen = { ...baseDish, name: "Tokyo Classic", dish_format: "ramen" };
  assert.equal(matches(ramen, "ramen"), true);
  assert.equal(matches({ ...ramen, dish_format: "pasta" }, "ramen"), false);
  // Multi-word enum values search as plain words.
  const noodles = { ...baseDish, name: "House Special", dish_format: "noodle_dish" };
  assert.equal(matches(noodles, "noodle dish"), true);
});

test("fallback formats never leak into search", () => {
  assert.equal(matches({ ...baseDish, dish_format: "other" }, "other"), false);
  assert.equal(matches({ ...baseDish, cooking_method: "mixed" }, "mixed"), false);
});

test("spice level answers 'spicy' without the word on the menu", () => {
  const hot = { ...baseDish, name: "Dan Dan Noodles", spice_level: "hot" };
  const mild = { ...baseDish, name: "Coconut Curry", spice_level: "none" };
  assert.equal(matches(hot, "spicy"), true);
  assert.equal(matches(mild, "spicy"), false);
});

test("cooking method is searchable", () => {
  const fried = { ...baseDish, name: "Crispy Tofu", cooking_method: "fried" };
  assert.equal(matches(fried, "fried"), true);
  assert.equal(
    matches({ ...baseDish, cooking_method: "stir_fry" }, "stir fry"),
    true
  );
});

test("ingredient_tags are searchable and boost relevance", () => {
  const dish = {
    ...baseDish,
    name: "Buddha Plate",
    ingredient_tags: ["chickpea", "tahini"],
  };
  assert.equal(matches(dish, "chickpea"), true);
  const intent = parseDishQuery("chickpea");
  const tagged = dishSearchScore(dish, "chickpea", intent, buildDishSearchIndex(dish));
  const untagged = dishSearchScore(baseDish, "chickpea", intent, buildDishSearchIndex(baseDish));
  assert.ok(tagged > untagged);
});

test("egg/soy/sesame-free intents are strict: only confirmed 'free' passes", () => {
  const dish = { ...baseDish, name: "Pad See Ew", egg_status: "free", soy_status: "contains" };
  assert.equal(matches(dish, "egg free"), true);
  assert.equal(matches(dish, "no egg"), true);
  assert.equal(matches(dish, "soy free"), false);
  assert.equal(matches({ ...dish, sesame_status: "unclear" }, "sesame free"), false);
  const intent = parseDishQuery("egg free soy free sesame free");
  assert.deepEqual(intent.terms, []);
  assert.deepEqual(queryIntentLabels(intent), ["egg-free", "soy-free", "sesame-free"]);
});

test("vegetarian intent uses vegetarian_status with a vegan-verdict fallback", () => {
  const enriched = {
    ...baseDish,
    name: "Paneer Tikka",
    verdict: "not_vegan",
    vegetarian_status: "vegetarian",
  };
  const meaty = { ...enriched, name: "Chicken Tikka", vegetarian_status: "not_vegetarian" };
  const unenriched = { ...baseDish, vegetarian_status: undefined };
  assert.equal(matches(enriched, "vegetarian"), true);
  assert.equal(matches(meaty, "vegetarian"), false);
  assert.equal(matches(unenriched, "vegetarian"), true);
  // "vegetarian" must not trip the vegan-friendly intent.
  assert.equal(parseDishQuery("vegetarian").veganFriendly, false);
});

test("intent words combine with ordinary text terms", () => {
  const dish = {
    ...baseDish,
    name: "Tofu Ramen",
    dish_format: "ramen",
    spice_level: "medium",
    egg_status: "free",
    ingredient_tags: ["tofu"],
  };
  assert.equal(matches(dish, "spicy egg free ramen"), true);
  assert.equal(matches({ ...dish, spice_level: "none" }, "spicy egg free ramen"), false);
});
