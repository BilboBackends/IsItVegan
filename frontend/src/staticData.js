// Static-hosting mode (GitHub Pages): the public site has NO backend — no
// Flask, no pipeline, no credentials. Consumer data is served as JSON
// snapshots exported by publish_static.py and committed to the repo, so the
// site updates when new data is published, and there is nothing a visitor
// can trigger, spend, or modify.
import { appendDataVersion, staticAssetUrl } from "./staticAssetUrls.js";

export const STATIC_MODE = import.meta.env.VITE_STATIC_DATA === "1";

// Respects Vite's `base` (GitHub Pages serves under /IsItVegan/).
const BASE = import.meta.env.BASE_URL;

let manifestPromise = null;

export function apiUrl(path) {
  if (!STATIC_MODE) return path;
  return staticAssetUrl(path, BASE);
}

/** Read the one shared snapshot generation for all static consumer assets. */
export function loadDataManifest() {
  if (!STATIC_MODE) return Promise.resolve(null);
  if (manifestPromise) return manifestPromise;
  manifestPromise = fetch(apiUrl("/api/data-manifest"), { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) return null;
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("json")) return null;
      return response.json();
    })
    .catch(() => null);
  return manifestPromise;
}

async function dataVersion() {
  const manifest = await loadDataManifest();
  return (
    manifest?.data_version ||
    manifest?.dishes_version ||
    manifest?.published_at ||
    null
  );
}

/** Fetch one member of a static snapshot generation without mixed caching. */
export async function fetchDataSnapshot(path, options) {
  if (!STATIC_MODE) return fetch(path, options);
  const version = await dataVersion();
  return fetch(appendDataVersion(apiUrl(path), version), options);
}

export function fetchRestaurants(options) {
  return fetchDataSnapshot("/api/restaurants", options);
}

// Tests and explicit page-level recovery can discard a failed manifest read.
export function clearDataManifestCache() {
  manifestPromise = null;
}

// Static exports provide full per-restaurant menu rows. Returns a
// Response-shaped object so DishModal's fetch handling stays unchanged.
export async function fetchRestaurantDishes(restaurantId) {
  if (!STATIC_MODE) return fetch(`/api/restaurants/${restaurantId}/dishes`);

  // Newer exports publish one small file per restaurant. Fall back to the
  // legacy global snapshot so older deployments and local fixtures keep
  // working while the sharded format rolls out. Production Cloudflare
  // deploys omit the oversized plain snapshot, so current exports must always
  // contain the requested shard.
  const version = await dataVersion();
  const shard = await fetch(
    appendDataVersion(
      `${BASE}data/restaurant-dishes/${restaurantId}.json`,
      version
    )
  );
  const shardContentType = shard.headers.get("content-type") || "";
  if (shard.ok && shardContentType.includes("json")) return shard;
  if (!shard.ok && shard.status !== 404) return shard;

  const res = await fetchDataSnapshot("/api/dishes");
  const fallbackContentType = res.headers.get("content-type") || "";
  if (!res.ok || !fallbackContentType.includes("json")) {
    return {
      ok: false,
      status: res.ok ? 404 : res.status,
      json: async () => ({ dishes: [] }),
    };
  }
  const data = await res.json();
  const dishes = (data.dishes || []).filter(
    (dish) => dish.restaurant_id === restaurantId
  );
  return { ok: true, json: async () => ({ dishes }) };
}
