import test from "node:test";
import assert from "node:assert/strict";

import {
  ADAPTABLE,
  LIKELY,
  NO,
  SUITS,
  UNKNOWN,
  suitabilityTier,
} from "../src/dietProfile.js";

test("vegan profile mirrors the verdict taxonomy and is the default", () => {
  assert.equal(suitabilityTier({ verdict: "vegan" }), SUITS);
  assert.equal(suitabilityTier({ verdict: "likely_vegan" }), LIKELY);
  assert.equal(suitabilityTier({ verdict: "vegan_adaptable" }), ADAPTABLE);
  assert.equal(suitabilityTier({ verdict: "unclear" }), UNKNOWN);
  assert.equal(suitabilityTier({ verdict: "not_vegan" }), NO);
  assert.equal(suitabilityTier({ verdict: null }), UNKNOWN);
  assert.equal(suitabilityTier(null), UNKNOWN);
});

test("vegetarian profile reads vegetarian_status first", () => {
  // Cheese pizza: not vegan, explicitly vegetarian.
  assert.equal(
    suitabilityTier(
      { verdict: "not_vegan", vegetarian_status: "vegetarian" },
      "vegetarian"
    ),
    SUITS
  );
  assert.equal(
    suitabilityTier(
      { verdict: "not_vegan", vegetarian_status: "not_vegetarian" },
      "vegetarian"
    ),
    NO
  );
});

test("vegetarian profile falls back conservatively without enrichment", () => {
  assert.equal(suitabilityTier({ verdict: "vegan" }, "vegetarian"), SUITS);
  assert.equal(suitabilityTier({ verdict: "likely_vegan" }, "vegetarian"), LIKELY);
  // Adaptable might mean "hold the bacon" — never SUITS without status.
  assert.equal(
    suitabilityTier({ verdict: "vegan_adaptable" }, "vegetarian"),
    LIKELY
  );
  assert.equal(suitabilityTier({ verdict: "not_vegan" }, "vegetarian"), UNKNOWN);
});

test("pescatarian profile allows fish and shellfish only", () => {
  const fishDish = {
    verdict: "not_vegan",
    vegetarian_status: "not_vegetarian",
    meat_sources: ["fish"],
  };
  assert.equal(suitabilityTier(fishDish, "pescatarian"), SUITS);
  assert.equal(
    suitabilityTier({ ...fishDish, meat_sources: ["fish", "shellfish"] }, "pescatarian"),
    SUITS
  );
  // Surf and turf: beef disqualifies regardless of the fish.
  assert.equal(
    suitabilityTier({ ...fishDish, meat_sources: ["shellfish", "beef"] }, "pescatarian"),
    NO
  );
  // Meat implied but never named: unknown, not a recommendation.
  assert.equal(
    suitabilityTier({ ...fishDish, meat_sources: [] }, "pescatarian"),
    UNKNOWN
  );
  // Vegetarian food suits pescatarians.
  assert.equal(
    suitabilityTier(
      { verdict: "not_vegan", vegetarian_status: "vegetarian", meat_sources: [] },
      "pescatarian"
    ),
    SUITS
  );
});
