import { useEffect, useState } from "react";
import DishExplore from "./DishExplore.jsx";
import Explore from "./Explore.jsx";
import SavedExplore from "./SavedExplore.jsx";

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

  useEffect(() => {
    localStorage.setItem("veganfind:favorites", JSON.stringify(favorites));
  }, [favorites]);

  function toggle(kind, id) {
    setFavorites((current) => {
      const values = current[kind];
      return {
        ...current,
        [kind]: values.includes(id)
          ? values.filter((value) => value !== id)
          : [...values, id],
      };
    });
  }

  return (
    <div>
      <div className="mx-auto max-w-7xl px-4 pt-8">
        <div className="mb-5">
          <h1 className="text-2xl font-extrabold tracking-tight text-stone-900 sm:text-4xl">
            Find <span className="text-emerald-700">vegan-friendly</span> food near you
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-stone-500">
            Search every analyzed menu at once, or browse nearby restaurants on the map.
            Every verdict is backed by menu evidence.
          </p>
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
