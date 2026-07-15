import {
  fetchDataSnapshot,
  fetchRestaurantDishes,
  STATIC_MODE,
} from "./staticData.js";
import { readGzipJson } from "./gzipJson.js";
import { registerDishes } from "./cloud.js";
import {
  settleDishShardLoads,
  stampRestaurantId,
} from "./dishShardBatch.js";
const CACHE_TTL_MS = 30_000;

let cachedDishes = null;
let cachedAt = 0;
let pendingRequest = null;
const shardCache = new Map();
const pendingShards = new Map();

/**
 * Share the large dish response between Explore and Saved while keeping it
 * fresh enough to reflect newly completed classifications. Concurrent mounts
 * also reuse the same request instead of downloading the payload twice.
 */
export function loadDishes() {
  // Static snapshots cannot change without a new page deployment. Keep the
  // expensive parsed index for this page session; local/API mode retains its
  // short freshness window so completed classifications appear promptly.
  if (
    cachedDishes &&
    (STATIC_MODE || Date.now() - cachedAt < CACHE_TTL_MS)
  ) {
    return Promise.resolve(cachedDishes);
  }
  if (pendingRequest) return pendingRequest;

  pendingRequest = loadDishPayload()
    .then((data) => {
      cachedDishes = data.dishes || [];
      cachedAt = Date.now();
      // Cloud favorites/votes key on stable identities built from these rows.
      registerDishes(cachedDishes);
      return cachedDishes;
    })
    .finally(() => {
      pendingRequest = null;
    });

  return pendingRequest;
}

async function loadDishPayload() {
  // GitHub Pages does not content-encode JSON responses, so the global food
  // index used to transfer tens of megabytes. A pre-compressed static asset
  // keeps the same in-browser search model while the local/API mode remains
  // behind /api/dishes and can later become a paginated PostgreSQL query.
  if (STATIC_MODE && typeof DecompressionStream === "function") {
    try {
      const response = await fetchDataSnapshot("/api/dishes.gz");
      if (response.ok) return await readGzipJson(response);
    } catch {
      // Older browsers, older deployments, or a corrupt cached asset fall
      // through to the plain snapshot instead of breaking Food or Saved.
    }
  }

  const response = await fetchDataSnapshot("/api/dishes");
  const contentType = response.headers.get("content-type") || "";
  if (!response.ok || (STATIC_MODE && !contentType.includes("json"))) {
    if (STATIC_MODE) throw staticCatalogError();
    throw new Error(`API ${response.status}`);
  }
  try {
    return await response.json();
  } catch (error) {
    if (STATIC_MODE) throw staticCatalogError();
    throw error;
  }
}

function staticCatalogError() {
  return new Error(
    "The compressed dish catalog could not be loaded. " +
      "Cloudflare intentionally omits the oversized plain JSON fallback."
  );
}

/** Load and register full menu rows for a small set of restaurants. */
export async function loadRestaurantDishShards(restaurantIds) {
  const result = await loadRestaurantDishShardBatch(restaurantIds);
  return result.dishes;
}

export async function loadRestaurantDishShardBatch(restaurantIds) {
  const uniqueIds = [...new Set((restaurantIds || []).map(Number))].filter(
    Number.isFinite
  );
  return settleDishShardLoads(uniqueIds, loadRestaurantDishShard);
}

async function loadRestaurantDishShard(restaurantId) {
  if (shardCache.has(restaurantId)) return shardCache.get(restaurantId);
  if (pendingShards.has(restaurantId)) return pendingShards.get(restaurantId);

  const request = fetchRestaurantDishes(restaurantId)
    .then(async (response) => {
      if (!response.ok) throw new Error(`API ${response.status}`);
      const contentType = response.headers?.get("content-type") || "";
      if (STATIC_MODE && !contentType.includes("json")) {
        throw new Error(
          `Menu shard ${restaurantId} returned the site shell instead of JSON.`
        );
      }
      const data = await response.json();
      // Local/API menu rows historically omit restaurant_id; stamp the
      // request identity before cloud favorite registration. Static shards
      // already contain it and are left unchanged.
      const dishes = stampRestaurantId(data.dishes || [], restaurantId);
      shardCache.set(restaurantId, dishes);
      registerDishes(dishes);
      return dishes;
    })
    .finally(() => pendingShards.delete(restaurantId));
  pendingShards.set(restaurantId, request);
  return request;
}

/**
 * Resolve numeric saved ids through restaurants.json and fetch only their
 * menu shards. Older static exports and local/API mode fall back to the
 * global response because they do not carry the dish_ids locator yet.
 */
export async function loadSavedDishes(dishIds, restaurants = []) {
  const result = await loadSavedDishesResult(dishIds, restaurants);
  return result.dishes;
}

export async function loadSavedDishesResult(dishIds, restaurants = []) {
  const ids = new Set((dishIds || []).map(Number));
  if (ids.size === 0) return { dishes: [], failures: [] };

  if (
    STATIC_MODE &&
    restaurants.length > 0 &&
    restaurants.every((item) => Array.isArray(item.dish_ids))
  ) {
    const restaurantIds = restaurants
      .filter((restaurant) => restaurant.dish_ids.some((id) => ids.has(Number(id))))
      .map((restaurant) => restaurant.id);
    const result = await loadRestaurantDishShardBatch(restaurantIds);
    return {
      dishes: result.dishes.filter((dish) => ids.has(Number(dish.id))),
      failures: result.failures,
    };
  }

  const dishes = await loadDishes();
  return {
    dishes: dishes.filter((dish) => ids.has(Number(dish.id))),
    failures: [],
  };
}

export function clearDishCache() {
  cachedDishes = null;
  cachedAt = 0;
  shardCache.clear();
  pendingShards.clear();
}
