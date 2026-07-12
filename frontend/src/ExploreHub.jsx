import { useContext, useEffect, useRef, useState } from "react";
import DishExplore from "./DishExplore.jsx";
import Explore from "./Explore.jsx";
import SavedExplore from "./SavedExplore.jsx";
import { loadDishes } from "./dishData.js";
import { apiUrl } from "./staticData.js";
import {
  SessionContext,
  mergeLocalFavorites,
  pullFavorites,
  registerRestaurants,
  syncFavorite,
} from "./cloud.js";

const EMPTY_FAVORITES = { dishes: [], restaurants: [] };

function loadFavorites() {
  try {
    const value = JSON.parse(localStorage.getItem("veganfind:favorites"));
    return {
      dishes: Array.isArray(value?.dishes) ? value.dishes : [],
      restaurants: Array.isArray(value?.restaurants) ? value.restaurants : [],
    };
  } catch {
    return EMPTY_FAVORITES;
  }
}

export default function ExploreHub({ view = "restaurants" }) {
  const foodActive = view === "food";
  const savedActive = view === "saved";
  const [favorites, setFavorites] = useState(loadFavorites);
  const session = useContext(SessionContext);
  const syncedUserRef = useRef(null);

  useEffect(() => {
    localStorage.setItem("veganfind:favorites", JSON.stringify(favorites));
  }, [favorites]);

  // On sign-in: upload anything saved anonymously in this browser, then pull
  // the account's favorites and union them in — no heart is ever lost. The
  // datasets load first so favorites can carry stable keys (place_id + dish
  // name) instead of the renumber-prone numeric ids.
  useEffect(() => {
    const userId = session?.user?.id;
    if (!userId || syncedUserRef.current === userId) return;
    syncedUserRef.current = userId;
    (async () => {
      try {
        const [restaurantsRes] = await Promise.all([
          fetch(apiUrl("/api/restaurants")),
          loadDishes(), // registers dishes as a side effect
        ]);
        if (restaurantsRes.ok) {
          const data = await restaurantsRes.json();
          registerRestaurants(data.restaurants || []);
        }
        await mergeLocalFavorites(loadFavorites(), userId);
        const cloud = await pullFavorites();
        if (cloud) {
          setFavorites((current) => ({
            dishes: [...new Set([...current.dishes, ...cloud.dishes])],
            restaurants: [
              ...new Set([...current.restaurants, ...cloud.restaurants]),
            ],
          }));
        }
      } catch {
        /* cloud sync is additive; local favorites keep working regardless */
      }
    })();
  }, [session]);

  function toggle(kind, id) {
    setFavorites((current) => {
      const values = current[kind];
      const active = !values.includes(id);
      syncFavorite(kind, id, active, session?.user?.id);
      return {
        ...current,
        [kind]: active
          ? [...values, id]
          : values.filter((value) => value !== id),
      };
    });
  }

  return (
    <div>
      <div className="mx-auto max-w-7xl px-4 pt-8">
        <div className="mb-5">
          <h1 className="text-2xl font-extrabold tracking-tight text-stone-900 sm:text-4xl">
            Tune your search to find <span className="text-emerald-700">vegan food</span> you&rsquo;ll love
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-stone-500">
            DishTune searches analyzed restaurant menus. Customize your search by location,
            distance, cuisine, price, and what&rsquo;s open—or look for a specific dish. Every
            verdict is backed by menu evidence.
          </p>
          <div className="mt-3 inline-flex items-center gap-1.5 text-xs font-semibold text-stone-500">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              className="h-3.5 w-3.5 text-emerald-700"
              aria-hidden="true"
            >
              <path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z" />
              <circle cx="12" cy="10" r="2.5" />
            </svg>
            <span>Currently serving Greater Orlando</span>
          </div>
        </div>
      </div>

      <div className="sticky top-[57px] z-20 border-y border-stone-200/80 bg-[#faf8f4]/95 py-2 backdrop-blur">
        <div className="mx-auto max-w-7xl overflow-x-auto px-4">
          <div className="inline-flex min-w-max rounded-xl border border-stone-200 bg-white p-1 shadow-sm">
          <a
            href="#restaurants"
            className={`rounded-lg px-4 py-2 text-sm font-bold transition sm:px-5 ${
              !foodActive && !savedActive
                ? "bg-emerald-700 text-white shadow-sm"
                : "text-stone-500 hover:bg-stone-50 hover:text-stone-800"
            }`}
          >
            Restaurants
          </a>
          <a
            href="#dishes"
            className={`rounded-lg px-4 py-2 text-sm font-bold transition sm:px-5 ${
              foodActive
                ? "bg-emerald-700 text-white shadow-sm"
                : "text-stone-500 hover:bg-stone-50 hover:text-stone-800"
            }`}
          >
            Dishes
          </a>
          <a
            href="#saved"
            className={`rounded-lg px-4 py-2 text-sm font-bold transition sm:px-5 ${
              savedActive
                ? "bg-emerald-700 text-white shadow-sm"
                : "text-stone-500 hover:bg-stone-50 hover:text-stone-800"
            }`}
          >
            Saved {favorites.dishes.length + favorites.restaurants.length > 0 && `(${favorites.dishes.length + favorites.restaurants.length})`}
          </a>
          </div>
        </div>
      </div>

      {savedActive ? (
        <SavedExplore
          favorites={favorites}
          toggleDish={(id) => toggle("dishes", id)}
          toggleRestaurant={(id) => toggle("restaurants", id)}
        />
      ) : foodActive ? (
        <DishExplore
          embedded
          favorites={favorites}
          toggleDish={(id) => toggle("dishes", id)}
          toggleRestaurant={(id) => toggle("restaurants", id)}
        />
      ) : (
        <Explore
          embedded
          favorites={favorites}
          toggleRestaurant={(id) => toggle("restaurants", id)}
          toggleDish={(id) => toggle("dishes", id)}
        />
      )}
    </div>
  );
}
