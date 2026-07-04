import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import DishDetail from "./DishDetail.jsx";
import DietaryBadges from "./DietaryBadges.jsx";
import FavoriteButton from "./FavoriteButton.jsx";
import { VerdictChip } from "./DishModal.jsx";
import RatingBadge, { ratingText } from "./RatingBadge.jsx";
import { FreshnessBadge, OpenStatusBadge, relativeDate } from "./RestaurantMeta.jsx";
import { cuisineLabel, cuisineOptions } from "./cuisine.js";
import { parsePriceValue } from "./price.js";
import {
  dishMatchesQuery,
  dishSearchScore,
  parseDishQuery,
  queryIntentLabels,
} from "./dishSearch.js";

const MAITLAND = { lat: 28.6278, lng: -81.3631 };
const RANGES = [
  { miles: 0, label: "Any distance" },
  { miles: 1, label: "Within 1 mi" },
  { miles: 2, label: "Within 2 mi" },
  { miles: 5, label: "Within 5 mi" },
  { miles: 10, label: "Within 10 mi" },
];

function haversineMiles(a, b) {
  const radius = 3958.8;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLng = ((b.lng - a.lng) * Math.PI) / 180;
  const value =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((a.lat * Math.PI) / 180) *
      Math.cos((b.lat * Math.PI) / 180) *
      Math.sin(dLng / 2) ** 2;
  return 2 * radius * Math.asin(Math.sqrt(value));
}

const VERDICTS = [
  { key: "all", label: "All verdicts" },
  { key: "vegan", label: "Vegan" },
  { key: "likely_vegan", label: "Likely vegan" },
  { key: "vegan_adaptable", label: "Adaptable" },
  { key: "unclear", label: "Unclear" },
  { key: "not_vegan", label: "Not vegan" },
];

const CATEGORIES = [
  { key: "food", label: "Food" },
  { key: "dessert", label: "Desserts" },
  { key: "drink", label: "Drinks" },
];

function prettyType(type) {
  if (!type) return null;
  return type.replaceAll("_", " ").replace(/\brestaurant\b/g, "").trim() || null;
}

function categoryOf(dish) {
  return dish.category === "drink" || dish.category === "dessert"
    ? dish.category
    : "food";
}

function splitReasoning(value) {
  if (!value) return { reasoning: null, evidence: null };
  const marker = " | evidence: ";
  const index = value.indexOf(marker);
  if (index === -1) return { reasoning: value, evidence: null };
  return {
    reasoning: value.slice(0, index),
    evidence: value.slice(index + marker.length),
  };
}

export default function DishExplore({
  embedded = false,
  favorites = { dishes: [], restaurants: [] },
  toggleDish = () => {},
  toggleRestaurant = () => {},
}) {
  const [dishes, setDishes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [verdict, setVerdict] = useState("all");
  const [category, setCategory] = useState("food");
  const [servingRole, setServingRole] = useState("all"); // all | meal | side
  const [maxPrice, setMaxPrice] = useState(0); // 0 = any; else dollar cap
  const [restaurant, setRestaurant] = useState("all");
  const [cuisine, setCuisine] = useState("all");
  const [sortBy, setSortBy] = useState("name");
  const [maxMiles, setMaxMiles] = useState(0);
  const [origin, setOrigin] = useState(MAITLAND);
  const [originLabel, setOriginLabel] = useState("Maitland");
  const [locating, setLocating] = useState(false);
  const [selectedDishId, setSelectedDishId] = useState(null);
  const [mobileView, setMobileView] = useState("list");
  const [isDesktop, setIsDesktop] = useState(
    () => window.matchMedia("(min-width: 1024px)").matches
  );
  const [focus, setFocus] = useState(null);
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef({});

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const onChange = (event) => setIsDesktop(event.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const syncDishFromHash = () => {
      const match = window.location.hash.match(/^#dishes\?dish=(\d+)/);
      setSelectedDishId(match ? Number(match[1]) : null);
    };
    syncDishFromHash();
    window.addEventListener("hashchange", syncDishFromHash);
    return () => window.removeEventListener("hashchange", syncDishFromHash);
  }, []);

  function useMyLocation() {
    if (!navigator.geolocation) return;
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setOrigin({ lat: position.coords.latitude, lng: position.coords.longitude });
        setOriginLabel("your location");
        setLocating(false);
      },
      () => setLocating(false),
      { timeout: 8000 }
    );
  }

  useEffect(() => {
    fetch("/api/dishes")
      .then((res) => {
        if (!res.ok) throw new Error(`API ${res.status}`);
        return res.json();
      })
      .then((data) => setDishes(data.dishes || []))
      .catch((e) => setError(e.message || "Could not load the dish database."))
      .finally(() => setLoading(false));
  }, []);

  const restaurants = useMemo(() => {
    const byId = new Map();
    for (const dish of dishes) {
      byId.set(dish.restaurant_id, {
        id: dish.restaurant_id,
        name: dish.restaurant_name,
      });
    }
    return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [dishes]);

  const categoryCounts = useMemo(() => {
    const counts = { food: 0, dessert: 0, drink: 0 };
    for (const dish of dishes) {
      if (
        restaurant !== "all" &&
        String(dish.restaurant_id) !== restaurant
      ) {
        continue;
      }
      if (cuisine !== "all" && cuisineLabel(dish.primary_type) !== cuisine) {
        continue;
      }
      counts[categoryOf(dish)] += 1;
    }
    return counts;
  }, [dishes, restaurant, cuisine]);

  const cuisines = useMemo(() => cuisineOptions(dishes), [dishes]);

  const dishesWithDistance = useMemo(
    () =>
      dishes.map((dish) => ({
        ...dish,
        distance:
          dish.lat != null && dish.lng != null
            ? haversineMiles(origin, { lat: dish.lat, lng: dish.lng })
            : null,
        priceValue: parsePriceValue(dish.price),
      })),
    [dishes, origin]
  );

  const parsedQuery = useMemo(() => parseDishQuery(query), [query]);
  const queryIntent = useMemo(() => queryIntentLabels(parsedQuery), [parsedQuery]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out = dishesWithDistance.filter((dish) => {
      if (verdict !== "all" && dish.verdict !== verdict) return false;
      if (categoryOf(dish) !== category) return false;
      // Legacy/unclear roles count as meals so unclassified data isn't hidden.
      if (category === "food" && servingRole === "meal" && dish.serving_role === "side") return false;
      if (category === "food" && servingRole === "side" && dish.serving_role !== "side") return false;
      // A price cap only keeps dishes we can PRICE — "Market Price" and
      // unpriced items can't honestly claim to be under $15.
      if (maxPrice > 0 && (dish.priceValue == null || dish.priceValue > maxPrice)) return false;
      if (restaurant !== "all" && String(dish.restaurant_id) !== restaurant) return false;
      if (cuisine !== "all" && cuisineLabel(dish.primary_type) !== cuisine) return false;
      if (maxMiles > 0 && (dish.distance == null || dish.distance > maxMiles)) return false;
      if (!q) return true;
      return dishMatchesQuery(dish, parsedQuery);
    });

    return out.sort((a, b) => {
      if (q) {
        const relevance =
          dishSearchScore(b, query, parsedQuery) -
          dishSearchScore(a, query, parsedQuery);
        if (relevance) return relevance;
      }
      if (sortBy === "restaurant") {
        return (
          a.restaurant_name.localeCompare(b.restaurant_name) ||
          a.name.localeCompare(b.name)
        );
      }
      if (sortBy === "confidence") {
        return (b.confidence ?? -1) - (a.confidence ?? -1) || a.name.localeCompare(b.name);
      }
      if (sortBy === "rating") {
        return (
          (b.rating ?? -1) - (a.rating ?? -1) ||
          (b.user_rating_count ?? 0) - (a.user_rating_count ?? 0) ||
          a.name.localeCompare(b.name)
        );
      }
      if (sortBy === "distance") {
        return (a.distance ?? 1e9) - (b.distance ?? 1e9) || a.name.localeCompare(b.name);
      }
      if (sortBy === "price") {
        return (
          (a.priceValue ?? 1e9) - (b.priceValue ?? 1e9) ||
          a.name.localeCompare(b.name)
        );
      }
      return a.name.localeCompare(b.name) || a.restaurant_name.localeCompare(b.restaurant_name);
    });
  }, [
    dishesWithDistance,
    query,
    parsedQuery,
    verdict,
    category,
    servingRole,
    maxPrice,
    restaurant,
    cuisine,
    maxMiles,
    sortBy,
  ]);

  const selectedDish = dishesWithDistance.find((dish) => dish.id === selectedDishId) || null;

  const veganCount = dishes.filter((d) =>
    ["vegan", "likely_vegan", "vegan_adaptable"].includes(d.verdict)
  ).length;

  const selectedRestaurant =
    restaurant === "all"
      ? null
      : restaurants.find((item) => String(item.id) === restaurant);
  const hasActiveFilters =
    Boolean(query.trim()) || verdict !== "all" || servingRole !== "all" ||
    maxPrice > 0 || restaurant !== "all" || cuisine !== "all" || maxMiles > 0;

  const mappedRestaurants = useMemo(() => {
    const groups = new Map();
    for (const dish of shown) {
      if (dish.lat == null || dish.lng == null) continue;
      const existing = groups.get(dish.restaurant_id);
      if (existing) {
        existing.count += 1;
      } else {
        groups.set(dish.restaurant_id, {
          id: dish.restaurant_id,
          name: dish.restaurant_name,
          lat: dish.lat,
          lng: dish.lng,
          count: 1,
          distance: dish.distance,
          rating: dish.rating,
          userRatingCount: dish.user_rating_count,
        });
      }
    }
    return [...groups.values()];
  }, [shown]);

  const showMap = isDesktop || mobileView === "map";

  useEffect(() => {
    if (!showMap || !mapEl.current) return;

    const map = L.map(mapEl.current, { zoomControl: true });
    mapRef.current = map;
    markersRef.current = {};
    L.tileLayer(
      "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      {
        attribution: "© OpenStreetMap © CARTO",
        subdomains: "abcd",
        maxZoom: 19,
      }
    ).addTo(map);

    const bounds = [];
    L.marker([origin.lat, origin.lng], {
      icon: L.divIcon({
        className: "",
        html: '<div class="vf-pin vf-pin--dot" style="background:#2563eb"></div>',
        iconSize: [12, 12],
        iconAnchor: [6, 6],
      }),
      zIndexOffset: -100,
    })
      .addTo(map)
      .bindTooltip(originLabel, { className: "vf-tooltip", direction: "top" });

    for (const item of mappedRestaurants) {
      const icon = L.divIcon({
        className: "",
        html: `<div class="vf-pin" style="background:#047857">${item.count}</div>`,
        iconSize: [26, 26],
        iconAnchor: [13, 13],
      });
      const marker = L.marker([item.lat, item.lng], { icon }).addTo(map);
      markersRef.current[item.id] = marker;
      bounds.push([item.lat, item.lng]);

      const tip = document.createElement("span");
      tip.textContent = `${item.name} · ${item.count} matching item${item.count === 1 ? "" : "s"}`;
      marker.bindTooltip(tip, {
        className: "vf-tooltip",
        direction: "top",
        offset: [0, -10],
      });

      const popup = document.createElement("div");
      popup.style.minWidth = "180px";
      const title = document.createElement("div");
      title.style.cssText = "font-weight:700;font-size:14px";
      title.textContent = item.name;
      const count = document.createElement("div");
      count.style.cssText = "margin-top:3px;color:#57534e;font-size:12px";
      count.textContent = `${item.count} matching menu item${item.count === 1 ? "" : "s"}`;
      const distance = document.createElement("div");
      distance.style.cssText =
        "margin-top:3px;color:#78716c;font-size:12px;font-weight:600";
      distance.textContent = `${item.distance.toFixed(1)} mi from ${originLabel}`;
      popup.append(title, count, distance);
      const googleRating = ratingText(item.rating, item.userRatingCount);
      if (googleRating) {
        const rating = document.createElement("div");
        rating.style.cssText =
          "margin-top:3px;color:#78716c;font-size:12px;font-weight:600";
        rating.textContent = `${googleRating} Google`;
        popup.append(rating);
      }
      const button = document.createElement("button");
      button.textContent = `Show all ${item.count} in the list →`;
      button.style.cssText =
        "margin-top:6px;color:#047857;font-weight:700;cursor:pointer;background:none;border:none;padding:0;font-size:13px";
      button.onclick = () => {
        showRestaurantItems(item.id);
      };
      popup.append(button);
      marker.bindPopup(popup, { closeButton: false });
    }

    if (bounds.length > 0) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
    } else {
      map.setView([MAITLAND.lat, MAITLAND.lng], 13);
    }

    const resizeTimer = setTimeout(() => map.invalidateSize(), 100);
    return () => {
      clearTimeout(resizeTimer);
      map.remove();
      mapRef.current = null;
      markersRef.current = {};
    };
  }, [mappedRestaurants, showMap, origin, originLabel]);

  useEffect(() => {
    if (!focus) return;
    const map = mapRef.current;
    const marker = markersRef.current[focus.restaurantId];
    if (!map || !marker) return;
    map.flyTo(marker.getLatLng(), 16, { duration: 0.8 });
    const timer = setTimeout(() => marker.openPopup(), 850);
    return () => clearTimeout(timer);
  }, [focus, showMap]);

  function showRestaurantOnMap(dish) {
    if (dish.lat == null || dish.lng == null) return;
    if (!isDesktop) setMobileView("map");
    setFocus({ restaurantId: dish.restaurant_id, timestamp: Date.now() });
  }

  function showRestaurantItems(restaurantId) {
    setRestaurant(String(restaurantId));
    setCuisine("all");
    setQuery("");
    setVerdict("all");
    setServingRole("all");
    setMaxMiles(0);
    setSortBy("name");
    setMobileView("list");
  }

  function openDish(dish) {
    window.location.hash = `dishes?dish=${dish.id}`;
  }

  function closeDish() {
    window.location.hash = "dishes";
  }

  function showDishOnMap(dish) {
    closeDish();
    showRestaurantOnMap(dish);
  }

  function clearFilters() {
    setQuery("");
    setVerdict("all");
    setCategory("food");
    setServingRole("all");
    setMaxPrice(0);
    setRestaurant("all");
    setCuisine("all");
    setMaxMiles(0);
  }

  return (
    <div className={`mx-auto max-w-5xl px-4 ${embedded ? "pb-8 pt-5" : "py-8"}`}>
      {!embedded && <div className="mb-6">
        <div className="mb-2 inline-flex rounded-full bg-emerald-100 px-3 py-1 text-xs font-bold uppercase tracking-wide text-emerald-800">
          Search every menu at once
        </div>
        <h1 className="text-3xl font-extrabold tracking-tight text-stone-900 sm:text-4xl">
          What are you craving?
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-stone-500">
          Search {dishes.length.toLocaleString()} menu items from {restaurants.length} restaurants
          {veganCount > 0 && `, including ${veganCount.toLocaleString()} vegan-friendly options`}.
          Try a dish, ingredient, cuisine, or restaurant name.
        </p>
      </div>}

      <div className="sticky top-[113px] z-10 -mx-4 mb-5 border-y border-stone-200/70 bg-[#faf8f4]/95 px-4 py-3 backdrop-blur">
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <div className="relative min-w-64 flex-1">
              <span className="pointer-events-none absolute left-3 top-2.5 text-stone-400">⌕</span>
              <input
                autoFocus
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Try ‘vegan pizza’ or ‘high protein breakfast’…"
                className="w-full rounded-full border border-stone-300 bg-white py-2 pl-9 pr-4 text-sm shadow-sm outline-none placeholder:text-stone-400 focus:border-emerald-600 focus:ring-1 focus:ring-emerald-600"
              />
            </div>
            <select
              value={restaurant}
              onChange={(event) => {
                const value = event.target.value;
                if (value === "all") setRestaurant("all");
                else showRestaurantItems(value);
              }}
              className="max-w-52 rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
            >
              <option value="all">All restaurants</option>
              {restaurants.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
            <select
              value={cuisine}
              onChange={(event) => setCuisine(event.target.value)}
              className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
              aria-label="Filter by cuisine"
            >
              <option value="all">All cuisines</option>
              {cuisines.map((label) => (
                <option key={label} value={label}>{label}</option>
              ))}
            </select>
            {category === "food" && (
              <select
                value={servingRole}
                onChange={(e) => setServingRole(e.target.value)}
                className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
                aria-label="Filter by meals or sides"
                title="Full meals vs sides and small plates; items not yet reclassified count as meals"
              >
                <option value="all">Meals & sides</option>
                <option value="meal">Meals only</option>
                <option value="side">Sides & small plates</option>
              </select>
            )}
            <select
              value={maxPrice}
              onChange={(e) => setMaxPrice(Number(e.target.value))}
              className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
              aria-label="Filter by price"
              title="Caps by listed menu price; unpriced items are hidden while a cap is active"
            >
              <option value={0}>Any price</option>
              <option value={10}>Under $10</option>
              <option value={15}>Under $15</option>
              <option value={20}>Under $20</option>
              <option value={30}>Under $30</option>
            </select>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
            >
              <option value="name">Sort: Dish name</option>
              <option value="restaurant">Sort: Restaurant</option>
              <option value="confidence">Sort: Confidence</option>
              <option value="rating">Sort: Restaurant rating</option>
              <option value="distance">Sort: Closest</option>
              <option value="price">Sort: Cheapest</option>
            </select>
            <select
              value={maxMiles}
              onChange={(event) => setMaxMiles(Number(event.target.value))}
              className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
            >
              {RANGES.map((range) => (
                <option key={range.miles} value={range.miles}>{range.label}</option>
              ))}
            </select>
            <button
              onClick={useMyLocation}
              disabled={locating}
              className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm font-medium text-stone-700 shadow-sm hover:border-emerald-600 hover:text-emerald-700 disabled:text-stone-400"
              title={`Distances measured from ${originLabel}`}
            >
              {locating ? "Locating…" : "Near me"}
            </button>
            <div className="ml-auto flex overflow-hidden rounded-full border border-stone-300 bg-white shadow-sm lg:hidden">
              {["list", "map"].map((view) => (
                <button
                  key={view}
                  onClick={() => setMobileView(view)}
                  className={`px-4 py-2 text-sm font-semibold capitalize ${
                    mobileView === view ? "bg-emerald-700 text-white" : "text-stone-600"
                  }`}
                >
                  {view}
                </button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-stone-500">
            <span className="font-semibold">Try:</span>
            {["Pad thai", "Vegan pizza", "High protein breakfast", "Tofu", "Seitan", "Mushroom"].map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setQuery(example)}
                className="rounded-full border border-stone-200 bg-white px-2.5 py-1 hover:border-emerald-400 hover:text-emerald-700"
              >
                {example}
              </button>
            ))}
            {queryIntent.length > 0 && (
              <span className="ml-auto font-medium text-emerald-700">
                Understood: {queryIntent.join(" · ")}
              </span>
            )}
          </div>
          <div className="flex gap-2 overflow-x-auto pb-0.5">
            {CATEGORIES.map((item) => {
              const count = categoryCounts[item.key];
              return (
                <button
                  key={item.key}
                  onClick={() => {
                    setCategory(item.key);
                    if (item.key !== "food") setServingRole("all");
                  }}
                  className={`shrink-0 rounded-full px-3 py-1.5 text-sm font-bold transition ${
                    category === item.key
                      ? "bg-stone-800 text-white"
                      : "border border-stone-200 bg-white text-stone-600 hover:border-stone-400"
                  }`}
                >
                  {item.label}
                  <span className={`ml-1.5 text-xs ${category === item.key ? "text-stone-300" : "text-stone-400"}`}>
                    {count.toLocaleString()}
                  </span>
                </button>
              );
            })}
          </div>
          <div className="flex gap-2 overflow-x-auto pb-0.5">
            {VERDICTS.map((item) => (
              <button
                key={item.key}
                onClick={() => setVerdict(item.key)}
                className={`shrink-0 rounded-full px-3 py-1 text-xs font-semibold transition ${
                  verdict === item.key
                    ? "bg-emerald-700 text-white"
                    : "border border-stone-200 bg-white text-stone-600 hover:border-emerald-500"
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
          {hasActiveFilters && (
            <div
              className="flex flex-wrap items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2"
              aria-live="polite"
            >
              <span className="text-xs font-bold uppercase tracking-wide text-emerald-800">
                Active filters
              </span>
              {selectedRestaurant && (
                <button
                  onClick={() => setRestaurant("all")}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
                  title="Remove restaurant filter"
                >
                  Restaurant: {selectedRestaurant.name}
                  <span aria-hidden="true" className="text-base leading-none">×</span>
                </button>
              )}
              {query.trim() && (
                <button
                  onClick={() => setQuery("")}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
                  title="Remove search filter"
                >
                  Search: “{query.trim()}”
                  <span aria-hidden="true" className="text-base leading-none">×</span>
                </button>
              )}
              {verdict !== "all" && (
                <button
                  onClick={() => setVerdict("all")}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
                  title="Remove verdict filter"
                >
                  Verdict: {VERDICTS.find((item) => item.key === verdict)?.label}
                  <span aria-hidden="true" className="text-base leading-none">×</span>
                </button>
              )}
              {servingRole !== "all" && (
                <button
                  onClick={() => setServingRole("all")}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
                  title="Remove meal/side filter"
                >
                  Serving: {servingRole === "meal" ? "Meals only" : "Sides & small plates"}
                  <span aria-hidden="true" className="text-base leading-none">×</span>
                </button>
              )}
              {maxPrice > 0 && (
                <button
                  onClick={() => setMaxPrice(0)}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
                  title="Remove price filter"
                >
                  Price: Under ${maxPrice}
                  <span aria-hidden="true" className="text-base leading-none">×</span>
                </button>
              )}
              {cuisine !== "all" && (
                <button
                  onClick={() => setCuisine("all")}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
                  title="Remove cuisine filter"
                >
                  Cuisine: {cuisine}
                  <span aria-hidden="true" className="text-base leading-none">×</span>
                </button>
              )}
              {maxMiles > 0 && (
                <button
                  onClick={() => setMaxMiles(0)}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
                  title="Remove distance filter"
                >
                  Within {maxMiles} mi of {originLabel}
                  <span aria-hidden="true" className="text-base leading-none">×</span>
                </button>
              )}
              <button
                onClick={clearFilters}
                className="ml-auto text-xs font-bold text-emerald-800 underline decoration-emerald-400 underline-offset-2 hover:text-emerald-950"
              >
                Clear all
              </button>
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="lg:grid lg:grid-cols-2 lg:items-start lg:gap-5">
        <div className={!isDesktop && mobileView === "map" ? "hidden" : ""}>
          {loading ? (
            <div className="p-12 text-center text-stone-400">Loading the menu database…</div>
          ) : shown.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-stone-300 p-12 text-center">
              <div className="font-semibold text-stone-700">No dishes match those filters.</div>
              <button onClick={clearFilters} className="mt-2 text-sm font-bold text-emerald-700 hover:underline">
                Clear filters
              </button>
            </div>
          ) : (
            <>
          <div className="mb-2 flex items-center justify-between text-xs font-medium uppercase tracking-wide text-stone-400">
            <span>{shown.length.toLocaleString()} menu item{shown.length === 1 ? "" : "s"}</span>
            {(query || verdict !== "all" || category !== "food" || servingRole !== "all" || restaurant !== "all" || cuisine !== "all" || maxMiles > 0) && (
              <button onClick={clearFilters} className="normal-case tracking-normal text-emerald-700 hover:underline">
                Clear filters
              </button>
            )}
          </div>
          <ol className="overflow-hidden rounded-2xl border border-stone-200 bg-white shadow-sm divide-y divide-stone-100">
            {shown.map((dish) => {
              const details = splitReasoning(dish.reasoning);
              const cuisine = prettyType(dish.primary_type);
              return (
                <li key={dish.id} className="px-4 py-4 transition hover:bg-stone-50 sm:px-5">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          onClick={() => openDish(dish)}
                          className="text-left font-bold leading-snug text-stone-900 hover:text-emerald-700 hover:underline"
                        >
                          {dish.name}
                        </button>
                        {dish.price && <span className="text-sm font-medium text-stone-400">{dish.price}</span>}
                      </div>
                      {dish.raw_description && (
                        <p className="mt-1 text-sm leading-relaxed text-stone-600">{dish.raw_description}</p>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <VerdictChip verdict={dish.verdict} />
                      {dish.confidence != null && (
                        <span className="hidden text-xs tabular-nums text-stone-400 sm:inline">
                          {Math.round(dish.confidence * 100)}%
                        </span>
                      )}
                      <FavoriteButton
                        active={favorites.dishes.includes(dish.id)}
                        onClick={() => toggleDish(dish.id)}
                        label="dish"
                      />
                    </div>
                  </div>

                  <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                    <button
                      onClick={() => showRestaurantItems(dish.restaurant_id)}
                      className="rounded-full bg-emerald-50 px-2.5 py-1 font-bold text-emerald-800 hover:bg-emerald-100"
                      title={`Show every item from ${dish.restaurant_name}`}
                    >
                      {dish.restaurant_name}
                    </button>
                    {cuisine && <span className="rounded-full bg-stone-100 px-2.5 py-1 capitalize text-stone-600">{cuisine}</span>}
                    <span className="rounded-full bg-stone-100 px-2.5 py-1 capitalize text-stone-500">{categoryOf(dish)}</span>
                    <DietaryBadges dish={dish} includeMeals />
                    <RatingBadge
                      rating={dish.rating}
                      userRatingCount={dish.user_rating_count}
                      className="rounded-full bg-amber-50 px-2.5 py-1"
                    />
                    <OpenStatusBadge openNow={dish.open_now} enrichedAt={dish.enriched_at} />
                    <FreshnessBadge fetchedAt={dish.menu_fetched_at} compact />
                    {dish.serves_vegetarian === 1 && (
                      <span className="rounded-full bg-lime-50 px-2.5 py-1 font-medium text-lime-800">veg-friendly restaurant</span>
                    )}
                  </div>

                  {(details.reasoning || details.evidence) && (
                    <div className="mt-2 text-xs leading-relaxed text-stone-400">
                      {details.reasoning}
                      {details.evidence && (
                        <span className="ml-1 text-stone-500">Menu evidence: {details.evidence}</span>
                      )}
                    </div>
                  )}

                  <div className="mt-2 flex flex-wrap gap-3 text-xs">
                    {dish.distance != null && (
                      <span className="font-semibold text-stone-500">{dish.distance.toFixed(1)} mi from {originLabel}</span>
                    )}
                    {dish.address && <span className="text-stone-400">{dish.address}</span>}
                    {dish.lat != null && dish.lng != null && (
                      <button
                        onClick={() => showRestaurantOnMap(dish)}
                        className="font-semibold text-emerald-700 hover:underline"
                      >
                        Show on map
                      </button>
                    )}
                    {dish.website_url && (
                      <a href={dish.website_url} target="_blank" rel="noreferrer" className="font-semibold text-emerald-700 hover:underline">
                        Restaurant website ↗
                      </a>
                    )}
                    <button onClick={() => openDish(dish)} className="font-bold text-emerald-700 hover:underline">
                      Details & share
                    </button>
                  </div>
                </li>
              );
            })}
          </ol>
            </>
          )}
        </div>

        <div className={`${!isDesktop && mobileView === "list" ? "hidden" : ""} lg:sticky lg:top-32`}>
          <div className="relative z-0 isolate">
            <div
              ref={mapEl}
              className="h-[70vh] w-full overflow-hidden rounded-2xl border border-stone-200 shadow-sm lg:h-[calc(100vh-11rem)]"
            />
            <div className="pointer-events-none absolute bottom-4 left-4 z-[500] rounded-xl border border-stone-200 bg-white/95 px-3 py-2 text-xs font-medium text-stone-600 shadow-md">
              Pin numbers show matching menu items
            </div>
          </div>
        </div>
      </div>

      <p className="py-5 text-center text-xs text-stone-400">
        Verdicts are inferred from menu text. Confirm ingredients with the restaurant for allergies or strict diets.
      </p>

      {selectedDish && (
        <DishDetail
          dish={selectedDish}
          onClose={closeDish}
          onShowMap={showDishOnMap}
          favorite={favorites.dishes.includes(selectedDish.id)}
          onToggleFavorite={() => toggleDish(selectedDish.id)}
          restaurantFavorite={favorites.restaurants.includes(selectedDish.restaurant_id)}
          onToggleRestaurant={() => toggleRestaurant(selectedDish.restaurant_id)}
        />
      )}
    </div>
  );
}
