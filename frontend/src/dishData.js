import { apiUrl, STATIC_MODE } from "./staticData.js";
import { readGzipJson } from "./gzipJson.js";
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

  pendingRequest = loadDishPayload()
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

async function loadDishPayload() {
  // GitHub Pages does not content-encode JSON responses, so the global food
  // index used to transfer tens of megabytes. A pre-compressed static asset
  // keeps the same in-browser search model while the local/API mode remains
  // behind /api/dishes and can later become a paginated PostgreSQL query.
  if (STATIC_MODE && typeof DecompressionStream === "function") {
    try {
      const response = await fetch(apiUrl("/api/dishes.gz"));
      if (response.ok) return await readGzipJson(response);
    } catch {
      // Older browsers, older deployments, or a corrupt cached asset fall
      // through to the plain snapshot instead of breaking Food or Saved.
    }
  }

  const response = await fetch(apiUrl("/api/dishes"));
  if (!response.ok) throw new Error(`API ${response.status}`);
  return response.json();
}

export function clearDishCache() {
  cachedDishes = null;
  cachedAt = 0;
}
