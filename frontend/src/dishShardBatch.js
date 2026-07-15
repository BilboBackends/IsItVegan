export function stampRestaurantId(dishes, restaurantId) {
  return (dishes || []).map((dish) =>
    dish.restaurant_id == null
      ? { ...dish, restaurant_id: restaurantId }
      : dish
  );
}

/** Resolve independent menu loads without letting one bad shard erase all. */
export async function settleDishShardLoads(restaurantIds, loadOne) {
  const results = await Promise.allSettled(
    restaurantIds.map((restaurantId) => loadOne(restaurantId))
  );
  const dishes = [];
  const failures = [];
  results.forEach((result, index) => {
    const restaurantId = restaurantIds[index];
    if (result.status === "fulfilled") {
      dishes.push(...result.value);
      return;
    }
    failures.push({
      restaurantId,
      message: result.reason?.message || "Menu data could not be loaded.",
    });
  });
  return { dishes, failures };
}
