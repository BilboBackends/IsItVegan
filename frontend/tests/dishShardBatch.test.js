import test from "node:test";
import assert from "node:assert/strict";

import {
  settleDishShardLoads,
  stampRestaurantId,
} from "../src/dishShardBatch.js";

test("keeps successful menu shards when another restaurant fails", async () => {
  const result = await settleDishShardLoads([10, 20, 30], async (id) => {
    if (id === 20) throw new Error("missing shard");
    return [{ id: id + 1, restaurant_id: id, name: `Dish ${id}` }];
  });

  assert.deepEqual(
    result.dishes.map((dish) => dish.restaurant_id),
    [10, 30]
  );
  assert.deepEqual(result.failures, [
    { restaurantId: 20, message: "missing shard" },
  ]);
});

test("stamps local API menu rows with their requested restaurant", () => {
  const existing = { id: 1, restaurant_id: 9, name: "Existing" };
  const [stamped, unchanged] = stampRestaurantId(
    [{ id: 2, name: "Missing context" }, existing],
    7
  );

  assert.equal(stamped.restaurant_id, 7);
  assert.equal(unchanged, existing);
  assert.equal(unchanged.restaurant_id, 9);
});
