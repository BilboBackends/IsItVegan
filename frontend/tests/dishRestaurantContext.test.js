import test from "node:test";
import assert from "node:assert/strict";

import { withRestaurantContext } from "../src/dishRestaurantContext.js";

test("hydrates compact dish rows from the restaurant directory", () => {
  const hours = ["Monday: 9:00 AM-5:00 PM"];
  const dish = withRestaurantContext(
    { id: 7, restaurant_id: 3, name: "Tofu bowl" },
    {
      id: 3,
      name: "Cafe Test",
      place_id: "place-3",
      lat: 28.5,
      lng: -81.3,
      primary_type: "cafe",
      opening_hours: hours,
    }
  );
  assert.equal(dish.restaurant_name, "Cafe Test");
  assert.equal(dish.place_id, "place-3");
  assert.equal(dish.primary_type, "cafe");
  assert.equal(dish.opening_hours, hours);
});

test("keeps legacy metadata when no directory row is available", () => {
  const dish = { id: 7, restaurant_id: 3, restaurant_name: "Legacy Cafe" };
  assert.deepEqual(withRestaurantContext(dish, null), dish);
});
