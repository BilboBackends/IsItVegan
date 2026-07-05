import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { apiUrl } from "./staticData.js";
import FavoriteButton from "./FavoriteButton.jsx";
import RatingBadge from "./RatingBadge.jsx";
import { FreshnessBadge, OpenStatusBadge } from "./RestaurantMeta.jsx";
import { VerdictChip } from "./DishModal.jsx";
import { loadDishes } from "./dishData.js";

export default function SavedExplore({ favorites, toggleDish, toggleRestaurant }) {
  const [restaurants, setRestaurants] = useState([]);
  const [dishes, setDishes] = useState([]);
  const [loading, setLoading] = useState(true);
  const mapEl = useRef(null);
  const mapRef = useRef(null);

  useEffect(() => {
    Promise.all([
      fetch(apiUrl("/api/restaurants")).then((response) => response.json()),
      loadDishes(),
    ])
      .then(([restaurantData, dishData]) => {
        setRestaurants(restaurantData.restaurants || []);
        setDishes(dishData);
      })
      .finally(() => setLoading(false));
  }, []);

  const savedRestaurants = useMemo(
    () => restaurants.filter((item) => favorites.restaurants.includes(item.id)),
    [restaurants, favorites.restaurants]
  );
  const savedDishes = useMemo(
    () => dishes.filter((item) => favorites.dishes.includes(item.id)),
    [dishes, favorites.dishes]
  );

  // Saved map: emerald count-pins where the saved dishes are, stone pins
  // for saved restaurants with no saved dish. Rebuilt on every change —
  // trivial at favorites scale.
  useEffect(() => {
    if (loading || !mapEl.current) return;
    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
    }
    const groups = new Map();
    for (const dish of savedDishes) {
      if (dish.lat == null || dish.lng == null) continue;
      if (!groups.has(dish.restaurant_id)) {
        groups.set(dish.restaurant_id, {
          name: dish.restaurant_name,
          lat: dish.lat,
          lng: dish.lng,
          count: 0,
        });
      }
      groups.get(dish.restaurant_id).count += 1;
    }
    for (const restaurant of savedRestaurants) {
      if (restaurant.lat == null || restaurant.lng == null) continue;
      if (!groups.has(restaurant.id)) {
        groups.set(restaurant.id, {
          name: restaurant.name,
          lat: restaurant.lat,
          lng: restaurant.lng,
          count: 0,
        });
      }
    }
    const pins = [...groups.values()];
    if (pins.length === 0) return;
    const map = L.map(mapEl.current, { scrollWheelZoom: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors",
    }).addTo(map);
    map.fitBounds(pins.map((p) => [p.lat, p.lng]), {
      padding: [40, 40],
      maxZoom: 15,
    });
    for (const p of pins) {
      const isDishPin = p.count > 0;
      const marker = L.marker([p.lat, p.lng], {
        icon: L.divIcon({
          className: "",
          html: `<div style="display:flex;align-items:center;justify-content:center;background:${
            isDishPin ? "#047857" : "#78716c"
          };color:#fff;border:2px solid #fff;border-radius:9999px;min-width:22px;height:22px;padding:0 3px;font-size:11px;font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,.4)">${
            isDishPin ? p.count : "♥"
          }</div>`,
          iconSize: [22, 22],
          iconAnchor: [11, 11],
        }),
      }).addTo(map);
      marker.bindTooltip(
        isDishPin
          ? `${p.name} — ${p.count} saved item${p.count === 1 ? "" : "s"}`
          : `${p.name} — saved restaurant`,
        { direction: "top", offset: [0, -10] }
      );
    }
    mapRef.current = map;
    return () => {
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
    };
  }, [loading, savedDishes, savedRestaurants]);

  if (loading) {
    return <div className="mx-auto max-w-5xl p-12 text-center text-stone-400">Loading Saved…</div>;
  }

  return (
    <div className="mx-auto max-w-5xl px-4 pb-10 pt-5">
      {savedRestaurants.length === 0 && savedDishes.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-stone-300 p-12 text-center">
          <div className="text-lg font-bold text-stone-700">Nothing saved yet</div>
          <p className="mt-1 text-sm text-stone-500">
            Tap the heart on a restaurant or food item to keep it here.
          </p>
          <a href="#dishes" className="mt-4 inline-block font-bold text-emerald-700 hover:underline">
            Browse food items →
          </a>
        </div>
      ) : (
        <div className="space-y-8">
          {(savedDishes.some((d) => d.lat != null) ||
            savedRestaurants.some((r) => r.lat != null)) && (
            <div
              ref={mapEl}
              className="z-0 h-72 overflow-hidden rounded-2xl border border-stone-200 shadow-sm"
            />
          )}
          <section>
            <h2 className="mb-3 text-lg font-extrabold text-stone-900">
              Restaurants <span className="text-sm font-normal text-stone-400">{savedRestaurants.length}</span>
            </h2>
            {savedRestaurants.length === 0 ? (
              <p className="text-sm text-stone-400">No saved restaurants.</p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                {savedRestaurants.map((restaurant) => (
                  <div key={restaurant.id} className="rounded-2xl border border-stone-200 bg-white p-4 shadow-sm">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-bold text-stone-900">{restaurant.name}</div>
                        <div className="mt-0.5 text-xs text-stone-500">{restaurant.address}</div>
                      </div>
                      <FavoriteButton active onClick={() => toggleRestaurant(restaurant.id)} label="restaurant" />
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <RatingBadge rating={restaurant.rating} userRatingCount={restaurant.user_rating_count} />
                      <OpenStatusBadge
                        openNow={restaurant.open_now}
                        enrichedAt={restaurant.enriched_at}
                        openingHours={restaurant.opening_hours}
                      />
                      <FreshnessBadge fetchedAt={restaurant.menu_fetched_at} compact />
                    </div>
                    <div className="mt-3 flex gap-3 text-xs font-bold">
                      <a href={`#restaurants?restaurant=${restaurant.id}`} className="text-emerald-700 hover:underline">View on map</a>
                      {restaurant.website_url && (
                        <a href={restaurant.website_url} target="_blank" rel="noreferrer" className="text-stone-500 hover:underline">
                          Website ↗
                        </a>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section>
            <h2 className="mb-3 text-lg font-extrabold text-stone-900">
              Food items <span className="text-sm font-normal text-stone-400">{savedDishes.length}</span>
            </h2>
            {savedDishes.length === 0 ? (
              <p className="text-sm text-stone-400">No saved food items.</p>
            ) : (
              <div className="divide-y divide-stone-100 overflow-hidden rounded-2xl border border-stone-200 bg-white shadow-sm">
                {savedDishes.map((dish) => (
                  <div key={dish.id} className="flex items-start justify-between gap-4 p-4">
                    <div className="min-w-0">
                      <a href={`#dishes?dish=${dish.id}`} className="font-bold text-stone-900 hover:text-emerald-700 hover:underline">
                        {dish.name}
                      </a>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-stone-500">
                        <span>
                          <span className="font-medium text-stone-400">Restaurant:</span>{" "}
                          {dish.restaurant_name}
                        </span>
                        <VerdictChip verdict={dish.verdict} />
                        {dish.price && <span>{dish.price}</span>}
                      </div>
                    </div>
                    <FavoriteButton active onClick={() => toggleDish(dish.id)} label="dish" />
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
