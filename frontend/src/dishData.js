const CACHE_TTL_MS = 30_000;

let cachedDishes = null;
let cachedAt = 0;
let pendingRequest = null;

/**
 * Share the large dish response between Explore and Saved while keeping it
 * fresh enough to reflect newly completed classifications. Concurrent mounts
 * also reuse the same request instead of downloading the payload twice.
 */
export function loadDishes() {
  if (cachedDishes && Date.now() - cachedAt < CACHE_TTL_MS) {
    return Promise.resolve(cachedDishes);
  }
  if (pendingRequest) return pendingRequest;

  pendingRequest = fetch("/api/dishes")
    .then((response) => {
      if (!response.ok) throw new Error(`API ${response.status}`);
      return response.json();
    })
    .then((data) => {
      cachedDishes = data.dishes || [];
      cachedAt = Date.now();
      return cachedDishes;
    })
    .finally(() => {
      pendingRequest = null;
    });

  return pendingRequest;
}

export function clearDishCache() {
  cachedDishes = null;
  cachedAt = 0;
}
