// Static-hosting mode (GitHub Pages): the public site has NO backend — no
// Flask, no pipeline, no credentials. Consumer data is served as JSON
// snapshots exported by publish_static.py and committed to the repo, so the
// site updates when new data is published, and there is nothing a visitor
// can trigger, spend, or modify.
export const STATIC_MODE = import.meta.env.VITE_STATIC_DATA === "1";

// Respects Vite's `base` (GitHub Pages serves under /IsItVegan/).
const BASE = import.meta.env.BASE_URL;

export function apiUrl(path) {
  if (!STATIC_MODE) return path;
  if (path === "/api/restaurants") return `${BASE}data/restaurants.json`;
  if (path === "/api/dishes") return `${BASE}data/dishes.json`;
  if (path === "/api/dishes.gz") return `${BASE}data/dishes.json.gz`;
  return path;
}

// The per-restaurant dish endpoint is derived client-side from the full
// dish snapshot in static mode (same rows, same fields). Returns a
// Response-shaped object so DishModal's fetch handling stays unchanged.
export async function fetchRestaurantDishes(restaurantId) {
  if (!STATIC_MODE) return fetch(`/api/restaurants/${restaurantId}/dishes`);

  // Newer exports publish one small file per restaurant. Fall back to the
  // legacy all-dishes snapshot so older deployments and local fixtures keep
  // working while the data format rolls out.
  const shard = await fetch(`${BASE}data/restaurant-dishes/${restaurantId}.json`);
  if (shard.ok || shard.status !== 404) return shard;

  const res = await fetch(`${BASE}data/dishes.json`);
  if (!res.ok) return res;
  const data = await res.json();
  const dishes = (data.dishes || []).filter(
    (dish) => dish.restaurant_id === restaurantId
  );
  return { ok: true, json: async () => ({ dishes }) };
}
