import { Fragment, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import DishDetail from "./DishDetail.jsx";
import DishCommentBadge from "./DishCommentBadge.jsx";
import DietaryBadges from "./DietaryBadges.jsx";
import FavoriteButton from "./FavoriteButton.jsx";
import FilterSidebar from "./FilterSidebar.jsx";
import LocationPicker, { NearMeIconButton } from "./LocationPicker.jsx";
import ThumbVote from "./ThumbVote.jsx";
import DishModal, { VerdictChip } from "./DishModal.jsx";
import RatingBadge, { ratingText } from "./RatingBadge.jsx";
import {
  FreshnessBadge,
  relativeDate,
  restaurantOpenSnapshot,
} from "./RestaurantMeta.jsx";
import {
  cuisineLabel,
  cuisineOptions,
  venueKind,
  venueKindLabel,
} from "./cuisine.js";
import { calorieLabel } from "./calories.js";
import { parsePriceValue, priceLevelSymbol } from "./price.js";
import { isCountedVegan } from "./verdicts.js";
import { suitabilityTier } from "./dietProfile.js";
import VenueTypeLegend from "./VenueTypeLegend.jsx";
import VenueKindFilter from "./VenueKindFilter.jsx";
import MapLegend from "./MapLegend.jsx";
import {
  ORIGIN_PIN_ANCHOR,
  ORIGIN_PIN_HTML,
  ORIGIN_PIN_SIZE,
  VENUE_MARKER_ANCHOR,
  VENUE_MARKER_SIZE,
  venueMarkerHtml,
} from "./venueMarkers.js";
import {
  focusMapOnMarker,
  isFreshMapFocus,
  placeFocusZoom,
} from "./mapFocus.js";
import {
  MAP_INDIVIDUAL_MARKER_ZOOM,
  aggregateMapItems,
  clusterMarkerHtml,
  mapItemsForViewport,
  withPriorityMapItem,
} from "./mapAggregation.js";
import { withRestaurantContext } from "./dishRestaurantContext.js";
import {
  buildDishSearchIndex,
  dishMatchesQuery,
  dishSearchScore,
  parseDishQuery,
} from "./dishSearch.js";
import { loadDishes } from "./dishData.js";
import {
  CLOUD_ENABLED,
  dishKey,
  fetchDishMentionCounts,
  registerRestaurants,
} from "./cloud.js";
import { fetchRestaurants } from "./staticData.js";
import {
  isOwnedDishDetailRoute,
  pushDishDetailRoute,
  replaceHashRoute,
} from "./hashNavigation.js";

// Default distance origin: downtown Orlando, the center of gravity of the
// current coverage. originLabel === DEFAULT_ORIGIN_LABEL means the user
// hasn't picked their own origin yet (near-me or address search).
const ORLANDO = { lat: 28.5384, lng: -81.3789 };
const DEFAULT_ORIGIN_LABEL = "Orlando";
const RESULTS_PAGE_SIZE = 120;
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

// Allergen-avoidance pills. Field order mirrors the badge order on cards.
const AVOID_OPTIONS = [
  ["dairy_status", "Dairy"],
  ["gluten_status", "Gluten"],
  ["nut_status", "Nuts"],
  ["egg_status", "Egg"],
  ["soy_status", "Soy"],
  ["sesame_status", "Sesame"],
];

const SPICE_MATCHES = {
  none: new Set(["none"]),
  any_heat: new Set(["mild", "medium", "hot"]),
  hot: new Set(["medium", "hot"]),
};

function formatLabel(value) {
  return value ? value.replaceAll("_", " ") : "";
}


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

function buildDishMapPopup(item, originLabel, onShowItems, onOpenMenu) {
  const popup = document.createElement("div");
  popup.style.cssText = "min-width:200px;max-width:250px";
  const addSection = () => {
    const section = document.createElement("div");
    section.style.cssText = "padding:6px 0 5px;border-top:1px solid #e7e5e4";
    popup.append(section);
    return section;
  };

  const head = document.createElement("div");
  head.style.cssText = "padding-bottom:6px";
  const title = document.createElement("div");
  title.style.cssText = "font-weight:700;font-size:14px;color:#1c1917";
  title.textContent = item.name;
  const sub = document.createElement("div");
  sub.style.cssText =
    "color:#78716c;font-size:12px;text-transform:capitalize;margin-top:1px";
  sub.textContent =
    (prettyType(item.primaryType) || "restaurant") +
    (priceLevelSymbol(item.priceLevel)
      ? ` · ${priceLevelSymbol(item.priceLevel)}`
      : "");
  head.append(title, sub);
  if (item.address) {
    const address = document.createElement("a");
    address.style.cssText =
      "display:block;margin-top:2px;color:#0369a1;font-size:11px;line-height:1.35;text-decoration:underline;text-underline-offset:2px";
    address.textContent = item.address;
    address.href = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
      item.address || item.name || ""
    )}`;
    address.target = "_blank";
    address.rel = "noopener noreferrer";
    address.title = "Open address in Google Maps";
    head.append(address);
  }
  popup.append(head);

  const googleRating = ratingText(item.rating, item.userRatingCount);
  if (item.openState != null || item.todayHours || googleRating) {
    const status = addSection();
    if (item.openState != null || item.todayHours) {
      const row = document.createElement("div");
      row.style.cssText = "font-size:12px;font-weight:600;color:#57534e";
      if (item.openState != null) {
        const state = document.createElement("span");
        state.style.cssText = `font-weight:700;color:${
          item.openState ? "#047857" : "#be123c"
        }`;
        state.textContent = item.openState ? "Open now" : "Closed";
        row.append(state);
        if (item.todayHours) row.append(" · ");
      }
      if (item.todayHours) row.append(`Today: ${item.todayHours}`);
      status.append(row);
    }
    if (googleRating) {
      const rating = document.createElement("div");
      rating.style.cssText =
        "margin-top:2px;color:#78716c;font-size:12px;font-weight:600";
      rating.textContent = `${googleRating} Google`;
      status.append(rating);
    }
  }

  const matches = addSection();
  const count = document.createElement("div");
  count.style.cssText = "font-size:13px;font-weight:600;color:#047857";
  count.textContent = `${item.count} matching menu item${item.count === 1 ? "" : "s"}`;
  matches.append(count);
  if (item.distance != null) {
    const distance = document.createElement("div");
    distance.style.cssText = "margin-top:2px;color:#a8a29e;font-size:11px";
    distance.textContent = `${item.distance.toFixed(1)} mi from ${originLabel}`;
    matches.append(distance);
  }
  const listButton = document.createElement("button");
  listButton.textContent = `Show all ${item.count} in the list →`;
  listButton.style.cssText =
    "display:block;margin-top:4px;color:#047857;font-weight:700;cursor:pointer;background:none;border:none;padding:0;font-size:12px";
  listButton.onclick = onShowItems;
  matches.append(listButton);

  if (item.websiteUrl || item.restaurant) {
    const actions = addSection();
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:14px;align-items:center";
    if (item.websiteUrl) {
      const site = document.createElement("a");
      site.textContent = "Website ↗";
      site.style.cssText =
        "color:#57534e;font-weight:700;font-size:13px;text-decoration:none";
      site.href = item.websiteUrl;
      site.target = "_blank";
      site.rel = "noopener noreferrer";
      row.append(site);
    }
    if (item.restaurant) {
      const menuButton = document.createElement("button");
      menuButton.textContent = "See dishes →";
      menuButton.style.cssText =
        "margin-left:auto;color:#047857;font-weight:700;cursor:pointer;background:none;border:none;padding:0;font-size:13px";
      menuButton.onclick = onOpenMenu;
      row.append(menuButton);
    }
    actions.append(row);
  }
  return popup;
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
  const [directoryError, setDirectoryError] = useState(null);
  const [directoryRetry, setDirectoryRetry] = useState(0);
  const [query, setQuery] = useState("");
  const [placeType, setPlaceType] = useState("all");
  // Multi-select toggle sets: pick any combination (vegan + adaptable, or
  // food + desserts). Empty verdict set = all verdicts; type defaults to
  // food and empty = all types.
  const [verdicts, setVerdicts] = useState(() => new Set());
  const [categories, setCategories] = useState(() => new Set(["food"]));
  const [servingRole, setServingRole] = useState("all"); // all | meal | side
  const [dishFormat, setDishFormat] = useState("all"); // enrichment dish_format
  // Allergen avoidance is strict: with a pill active, only dishes whose
  // status is a confirmed "free" pass — "unclear" is not safe enough.
  const [avoid, setAvoid] = useState(() => new Set());
  const [spiceFilter, setSpiceFilter] = useState("all"); // all|none|any_heat|hot
  const [fakeMeat, setFakeMeat] = useState("all"); // all | only | exclude
  const [maxPrice, setMaxPrice] = useState(0); // 0 = any; else dollar cap
  const [restaurant, setRestaurant] = useState("all");
  const [cuisine, setCuisine] = useState("all");
  const [openFilter, setOpenFilter] = useState("all");
  const [sortBy, setSortBy] = useState("recommended");
  const [maxMiles, setMaxMiles] = useState(0);
  const [origin, setOrigin] = useState(ORLANDO);
  const [originLabel, setOriginLabel] = useState(DEFAULT_ORIGIN_LABEL);
  // Phones: the inline "distances from X · change" address row under the
  // search bar — origin control without opening the collapsed filters.
  const [originOpen, setOriginOpen] = useState(false);
  const [selectedDishId, setSelectedDishId] = useState(null);
  const [menuRestaurant, setMenuRestaurant] = useState(null);
  const [menuCommentTarget, setMenuCommentTarget] = useState(null);
  const [dishMentionCounts, setDishMentionCounts] = useState(() => new Map());
  const [restaurantDirectory, setRestaurantDirectory] = useState(
    () => new Map()
  );
  const [mobileView, setMobileView] = useState("list");
  // Keep the sidebar expanded once there is room for it. The map/list split
  // waits until a wider breakpoint so neither pane becomes cramped.
  const [filtersOpen, setFiltersOpen] = useState(
    () => window.matchMedia("(min-width: 1024px)").matches
  );
  const [isDesktop, setIsDesktop] = useState(
    () => window.matchMedia("(min-width: 1280px)").matches
  );
  const [focus, setFocus] = useState(null);
  const [visibleLimit, setVisibleLimit] = useState(RESULTS_PAGE_SIZE);
  const [statusNow, setStatusNow] = useState(() => new Date());
  const [mapZoom, setMapZoom] = useState(10);
  const [mapBounds, setMapBounds] = useState(null);
  // Rows are compact one-glance lines; tapping one expands its full detail.
  const [expandedIds, setExpandedIds] = useState(() => new Set());
  const mapEl = useRef(null);
  const mobileMapAnchorRef = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef({});
  const renderedMarkersRef = useRef(new Map());
  const markerLayerRef = useRef(null);
  const originMarkerRef = useRef(null);
  const didInitialMapFitRef = useRef(false);
  const lastMapOriginRef = useRef(null);
  const boundsSyncRef = useRef(null);
  const dishMapActionsRef = useRef(null);
  const loadMoreRef = useRef(null);
  const sortBeforeRestaurantRef = useRef(null);
  // True while the dessert place chip auto-switched the Type filter, so
  // leaving the chip can restore the food default without clobbering a
  // Type the user chose themselves.
  const dessertCategoriesAutoRef = useRef(false);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1280px)");
    const onChange = (event) => setIsDesktop(event.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setStatusNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const syncDishFromHash = () => {
      const match = window.location.hash.match(/^#dishes\?dish=(\d+)/);
      setSelectedDishId(match ? Number(match[1]) : null);
      if (match) setMenuRestaurant(null);
    };
    syncDishFromHash();
    window.addEventListener("hashchange", syncDishFromHash);
    window.addEventListener("popstate", syncDishFromHash);
    return () => {
      window.removeEventListener("hashchange", syncDishFromHash);
      window.removeEventListener("popstate", syncDishFromHash);
    };
  }, []);

  useEffect(() => {
    loadDishes()
      .then(setDishes)
      .catch((e) => setError(e.message || "Could not load the dish database."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchRestaurants()
      .then((response) => {
        if (!response.ok) throw new Error(`Restaurant directory ${response.status}`);
        return response.json();
      })
      .then((data) => {
        const list = data.restaurants || [];
        if (list.length === 0) throw new Error("Restaurant directory is empty");
        registerRestaurants(list);
        setDirectoryError(null);
        setRestaurantDirectory(
          new Map(list.map((item) => [item.id, item]))
        );
      })
      .catch((reason) =>
        setDirectoryError(
          reason?.message || "Could not load restaurant details."
        )
      );
  }, [directoryRetry]);

  useEffect(() => {
    if (!CLOUD_ENABLED) return;
    const refresh = () => {
      fetchDishMentionCounts()
        .then(setDishMentionCounts)
        .catch(() => {});
    };
    refresh();
    window.addEventListener("dishtune:comments-changed", refresh);
    return () =>
      window.removeEventListener("dishtune:comments-changed", refresh);
  }, []);

  const compactCatalogNeedsDirectory =
    dishes.length > 0 && dishes[0]?.restaurant_name == null;
  const restaurantContextReady =
    !compactCatalogNeedsDirectory || restaurantDirectory.size > 0;

  const restaurants = useMemo(() => {
    // The directory is canonical and can arrive before the global dish
    // catalog. In the normal path this avoids grouping 60k rows just to
    // reconstruct 700 restaurants.
    if (restaurantDirectory.size > 0) {
      return [...restaurantDirectory.values()].sort((a, b) =>
        (a.name || "").localeCompare(b.name || "")
      );
    }
    if (compactCatalogNeedsDirectory) return [];

    // Legacy/backend fallback for a full dish response without a directory.
    const byId = new Map();
    for (const dish of dishes) {
      let item = byId.get(dish.restaurant_id);
      if (!item) {
        item = {
          id: dish.restaurant_id,
          name: dish.restaurant_name || "Restaurant",
          place_id: dish.place_id,
          address: dish.address,
          website_url: dish.website_url,
          lat: dish.lat,
          lng: dish.lng,
          primary_type: dish.primary_type,
          price_level: dish.price_level,
          rating: dish.rating,
          user_rating_count: dish.user_rating_count,
          open_now: dish.open_now,
          opening_hours: dish.opening_hours,
          enriched_at: dish.enriched_at,
          menu_fetched_at: dish.menu_fetched_at,
          business_status: dish.business_status,
          vegan_options: 0,
          vegan_sides: 0,
        };
        byId.set(dish.restaurant_id, item);
      }
      if (categoryOf(dish) === "food" && isCountedVegan(dish)) {
        if (dish.serving_role === "side") item.vegan_sides += 1;
        else item.vegan_options += 1;
      }
    }
    return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [dishes, restaurantDirectory, compactCatalogNeedsDirectory]);

  const restaurantById = useMemo(
    () => new Map(restaurants.map((item) => [item.id, item])),
    [restaurants]
  );

  const restaurantOpenStates = useMemo(
    () =>
      new Map(
        restaurants.map((item) => [
          item.id,
          restaurantOpenSnapshot(
            item.open_now,
            item.enriched_at,
            item.opening_hours,
            statusNow
          ),
        ])
      ),
    [restaurants, statusNow]
  );
  const activeOpenStates = openFilter === "all" ? null : restaurantOpenStates;

  // Keep exactly one retained derived dish array: restaurant context,
  // distance, and parsed price are added in the same pass. Compact catalog
  // rows therefore do not balloon into two separate 60k-object copies.
  const derivedDishes = useMemo(
    () =>
      (restaurantContextReady ? dishes : []).map((dish) => {
        const hydrated = withRestaurantContext(
          dish,
          restaurantDirectory.get(dish.restaurant_id)
        );
        hydrated.distance =
          hydrated.lat != null && hydrated.lng != null
            ? haversineMiles(origin, {
                lat: hydrated.lat,
                lng: hydrated.lng,
              })
            : null;
        hydrated.priceValue = parsePriceValue(hydrated.price);
        return hydrated;
      }),
    [dishes, restaurantDirectory, origin, restaurantContextReady]
  );

  const categoryCounts = useMemo(() => {
    const counts = { food: 0, dessert: 0, drink: 0 };
    for (const dish of derivedDishes) {
      if (
        restaurant !== "all" &&
        String(dish.restaurant_id) !== restaurant
      ) {
        continue;
      }
      if (cuisine !== "all" && cuisineLabel(dish.primary_type) !== cuisine) {
        continue;
      }
      if (placeType !== "all" && venueKind(dish.primary_type) !== placeType) {
        continue;
      }
      if (
        openFilter !== "all" &&
        activeOpenStates?.get(dish.restaurant_id)?.openState !==
          (openFilter === "open")
      ) {
        continue;
      }
      counts[categoryOf(dish)] += 1;
    }
    return counts;
  }, [
    derivedDishes,
    restaurant,
    cuisine,
    placeType,
    openFilter,
    activeOpenStates,
  ]);

  const cuisines = useMemo(
    () => cuisineOptions(restaurants),
    [restaurants]
  );

  // Keep typing responsive while React computes a result set for the latest
  // settled query. Building normalized search strings for 60k dishes is
  // expensive, so do it only after the user actually searches.
  const deferredQuery = useDeferredValue(query);
  const searchActive = deferredQuery.trim().length > 0;
  const searchIndex = useMemo(
    () => {
      const index = new Map();
      if (!restaurantContextReady || !searchActive) return index;
      for (const dish of dishes) {
        const hydrated = withRestaurantContext(
          dish,
          restaurantDirectory.get(dish.restaurant_id)
        );
        index.set(dish.id, buildDishSearchIndex(hydrated));
      }
      return index;
    },
    [dishes, restaurantDirectory, restaurantContextReady, searchActive]
  );
  const parsedQuery = useMemo(() => parseDishQuery(deferredQuery), [deferredQuery]);

  // Meals/sides grouping and its filter only make sense when the list is
  // food alone — a mixed food+drinks view has no meaningful role headings.
  const foodOnly = categories.size === 1 && categories.has("food");

  function toggleVerdict(key) {
    if (key === "all") {
      setVerdicts(new Set());
      return;
    }
    setVerdicts((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleCategory(key) {
    setCategories((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // The Dessert place chip means "show me treats", but the Type filter
  // defaults to food only — which hid every actual dessert at dessert
  // venues. While Type is still its untouched default, follow the venue
  // kind (mirroring DishModal's dessert-first menus); a Type the user
  // picked explicitly is never overridden.
  function changeVenueKind(key) {
    if (key === "dessert" && placeType !== "dessert" && foodOnly) {
      dessertCategoriesAutoRef.current = true;
      setCategories(new Set(["dessert"]));
    } else if (key !== "dessert" && dessertCategoriesAutoRef.current) {
      dessertCategoriesAutoRef.current = false;
      if (categories.size === 1 && categories.has("dessert")) {
        setCategories(new Set(["food"]));
      }
    }
    setPlaceType(key);
  }

  const shown = useMemo(() => {
    const q = deferredQuery.trim().toLowerCase();
    const out = derivedDishes.filter((dish) => {
      if (verdicts.size > 0 && !verdicts.has(dish.verdict)) return false;
      if (categories.size > 0 && !categories.has(categoryOf(dish))) return false;
      // Legacy/unclear roles count as meals so unclassified data isn't
      // hidden. The meals/sides filter only applies to food dishes.
      if (categoryOf(dish) === "food") {
        if (servingRole === "meal" && dish.serving_role === "side") return false;
        if (servingRole === "side" && dish.serving_role !== "side") return false;
      }
      if (dishFormat !== "all" && dish.dish_format !== dishFormat) return false;
      for (const field of avoid) {
        if (dish[field] !== "free") return false;
      }
      if (
        spiceFilter !== "all" &&
        !SPICE_MATCHES[spiceFilter].has(dish.spice_level)
      ) return false;
      if (fakeMeat === "only" && dish.protein_source !== "meat_analogue") return false;
      if (fakeMeat === "exclude" && dish.protein_source === "meat_analogue") return false;
      // A price cap only keeps dishes we can PRICE — "Market Price" and
      // unpriced items can't honestly claim to be under $15.
      if (maxPrice > 0 && (dish.priceValue == null || dish.priceValue > maxPrice)) return false;
      if (restaurant !== "all" && String(dish.restaurant_id) !== restaurant) return false;
      if (cuisine !== "all" && cuisineLabel(dish.primary_type) !== cuisine) return false;
      if (placeType !== "all" && venueKind(dish.primary_type) !== placeType) return false;
      if (
        openFilter !== "all" &&
        activeOpenStates?.get(dish.restaurant_id)?.openState !==
          (openFilter === "open")
      ) return false;
      if (maxMiles > 0 && (dish.distance == null || dish.distance > maxMiles)) return false;
      if (!q) return true;
      return dishMatchesQuery(dish, parsedQuery, searchIndex.get(dish.id));
    });

    // The old comparator recalculated and renormalized both dishes on every
    // comparison (O(n log n) expensive text work). Compute each score once.
    const relevanceScores = q
      ? new Map(
          out.map((dish) => [
            dish.id,
            dishSearchScore(
              dish,
              deferredQuery,
              parsedQuery,
              searchIndex.get(dish.id)
            ),
          ])
        )
      : null;

    // Whether the meals-before-sides ordering participates in this view.
    const roleOf = (dish) =>
      foodOnly && servingRole === "all"
        ? Number(dish.serving_role === "side")
        : 0;

    const sorted = out.sort((a, b) => {
      // Diet suitability (vegan today — see dietProfile.js) leads, and
      // relevance, "Closest", or "Cheapest" order dishes WITHIN a tier:
      // otherwise a strong text match on a beef noodle soup outranks the
      // vegan ramen the user actually came for. The one thing above it is
      // the browse view's meals/sides grouping — the section headings
      // promise two contiguous blocks, so the role split must stay the
      // outermost key. Searching skips that grouping (and its headings):
      // a relevance-ranked list isn't role-grouped.
      if (q) {
        const tierOrder = suitabilityTier(a) - suitabilityTier(b);
        if (tierOrder) return tierOrder;
        const relevance = relevanceScores.get(b.id) - relevanceScores.get(a.id);
        if (relevance) return relevance;
        const roleOrder = roleOf(a) - roleOf(b);
        if (roleOrder) return roleOrder;
      } else {
        const roleOrder = roleOf(a) - roleOf(b);
        if (roleOrder) return roleOrder;
        const tierOrder = suitabilityTier(a) - suitabilityTier(b);
        if (tierOrder) return tierOrder;
      }
      if (sortBy === "recommended") {
        return (
          (b.rating ?? -1) - (a.rating ?? -1) ||
          (b.confidence ?? -1) - (a.confidence ?? -1) ||
          a.name.localeCompare(b.name)
        );
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

    // Default browse interleaves restaurants: the quality order above still
    // decides who leads, but one well-rated menu may not monopolize the
    // first screens with dozens of consecutive rows. Each dish is keyed by
    // how many earlier dishes its restaurant already placed in its own
    // tier/role group; the stable re-sort then rotates restaurants while
    // preserving the quality order at equal counts.
    if (!q && sortBy === "recommended" && restaurant === "all") {
      const groupOf = (dish) =>
        `${suitabilityTier(dish)}|${roleOf(dish)}|${dish.restaurant_id}`;
      const seen = new Map();
      const occurrence = new Map();
      for (const dish of sorted) {
        const count = seen.get(groupOf(dish)) || 0;
        occurrence.set(dish.id, count);
        seen.set(groupOf(dish), count + 1);
      }
      sorted.sort(
        (a, b) =>
          roleOf(a) - roleOf(b) ||
          suitabilityTier(a) - suitabilityTier(b) ||
          occurrence.get(a.id) - occurrence.get(b.id)
      );
    }
    return sorted;
  }, [
    derivedDishes,
    deferredQuery,
    parsedQuery,
    searchIndex,
    verdicts,
    categories,
    foodOnly,
    servingRole,
    dishFormat,
    avoid,
    spiceFilter,
    fakeMeat,
    maxPrice,
    restaurant,
    cuisine,
    placeType,
    openFilter,
    activeOpenStates,
    maxMiles,
    sortBy,
  ]);

  useEffect(() => {
    setVisibleLimit(RESULTS_PAGE_SIZE);
  }, [
    deferredQuery,
    verdicts,
    categories,
    servingRole,
    dishFormat,
    avoid,
    spiceFilter,
    fakeMeat,
    maxPrice,
    restaurant,
    cuisine,
    placeType,
    openFilter,
    maxMiles,
    sortBy,
  ]);

  const visibleDishes = useMemo(
    () => shown.slice(0, visibleLimit),
    [shown, visibleLimit]
  );

  useEffect(() => {
    const target = loadMoreRef.current;
    if (mobileView !== "list" || !target || visibleLimit >= shown.length) {
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisibleLimit((current) =>
            Math.min(current + RESULTS_PAGE_SIZE, shown.length)
          );
        }
      },
      { rootMargin: "500px" }
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [shown.length, visibleLimit, mobileView]);

  const selectedDish =
    selectedDishId == null
      ? null
      : derivedDishes.find((dish) => dish.id === selectedDishId) || null;

  const veganCount = useMemo(
    () => dishes.filter(isCountedVegan).length,
    [dishes]
  );

  // Dish-type options come from the data so the dropdown never lists a
  // format with zero results. "other"/"unclear" stay out — they're
  // fallbacks, not something anyone craves.
  const dishFormats = useMemo(() => {
    const counts = new Map();
    for (const dish of dishes) {
      const format = dish.dish_format;
      if (!format || format === "unclear" || format === "other") continue;
      counts.set(format, (counts.get(format) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [dishes]);

  const selectedRestaurant =
    restaurant === "all"
      ? null
      : restaurants.find((item) => String(item.id) === restaurant);
  const categoriesActive = !foodOnly;
  const hasActiveFilters =
    verdicts.size > 0 || categoriesActive || servingRole !== "all" ||
    dishFormat !== "all" || avoid.size > 0 || spiceFilter !== "all" ||
    fakeMeat !== "all" ||
    maxPrice > 0 || restaurant !== "all" || cuisine !== "all" ||
    placeType !== "all" || openFilter !== "all" || maxMiles > 0;

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
          address: dish.address,
          lat: dish.lat,
          lng: dish.lng,
          count: 1,
          distance: dish.distance,
          rating: dish.rating,
          userRatingCount: dish.user_rating_count,
          primaryType: dish.primary_type,
          priceLevel: dish.price_level,
          websiteUrl: dish.website_url,
          restaurant: restaurantById.get(dish.restaurant_id),
        });
      }
    }
    return [...groups.values()];
  }, [shown, restaurantById]);

  // Marker popups can outlive the render that created them. Route their
  // filter action through the latest callback instead of capturing stale
  // restaurant/sort state.
  dishMapActionsRef.current = showRestaurantItems;

  const showMap = isDesktop || mobileView === "map";
  const focusedRestaurantId = focus?.restaurantId ?? null;

  function alignMobileMapViewport() {
    mobileMapAnchorRef.current?.scrollIntoView({
      block: "start",
      inline: "nearest",
      behavior: "auto",
    });
  }

  function enterMobileMap() {
    if (isDesktop) return;
    alignMobileMapViewport();
    setMobileView("map");
  }

  function enterMobileList() {
    mapRef.current?.stop();
    mapRef.current?.closePopup();
    setFocus(null);
    setMobileView("list");
  }

  useEffect(() => {
    if (isDesktop || mobileView !== "map") return;
    let secondFrame = null;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        alignMobileMapViewport();
        mapRef.current?.invalidateSize({ pan: false });
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame != null) window.cancelAnimationFrame(secondFrame);
    };
  }, [isDesktop, mobileView]);

  useEffect(() => {
    if (!showMap || !mapEl.current) return;

    let map = mapRef.current;
    if (!map) {
      map = L.map(mapEl.current, {
        zoomControl: true,
        zoomAnimation: isDesktop,
        fadeAnimation: isDesktop,
        markerZoomAnimation: isDesktop,
      });
      mapRef.current = map;
      markerLayerRef.current = L.layerGroup().addTo(map);
      L.tileLayer(
        isDesktop
          ? "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
          : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        {
          attribution: "© OpenStreetMap © CARTO",
          subdomains: "abcd",
          maxZoom: 19,
          keepBuffer: isDesktop ? 2 : 1,
          updateWhenIdle: !isDesktop,
          updateWhenZooming: isDesktop,
        }
      ).addTo(map);
      map.setView([origin.lat, origin.lng], 10);
      map.on("zoomend", () => setMapZoom(map.getZoom()));
    }
    markersRef.current = {};

    const bounds = mappedRestaurants.map((item) => [item.lat, item.lng]);
    // Origin marker (small blue pin). Drawn above venue pins so it stays
    // visible, but fully click-transparent (interactive: false) so taps on
    // an overlapping pin register on the pin; the origin label lives in the
    // filter panel, so the marker needs no tooltip of its own.
    if (!originMarkerRef.current) {
      originMarkerRef.current = L.marker([origin.lat, origin.lng], {
        icon: L.divIcon({
          className: "",
          html: ORIGIN_PIN_HTML,
          iconSize: ORIGIN_PIN_SIZE,
          iconAnchor: ORIGIN_PIN_ANCHOR,
        }),
        zIndexOffset: 1000,
        interactive: false,
      }).addTo(map);
    }
    originMarkerRef.current.setLatLng([origin.lat, origin.lng]);

    const focusedRestaurant =
      focusedRestaurantId == null
        ? null
        : mappedRestaurants.find(
            (item) => String(item.id) === String(focusedRestaurantId)
          );
    const markerItems = withPriorityMapItem(
      mapItemsForViewport(mappedRestaurants, mapZoom, mapBounds),
      focusedRestaurant
    );
    const entries = aggregateMapItems(
      markerItems,
      mapZoom,
      (item) => item.id,
      focusedRestaurant?.id
    );
    const activeMarkerKeys = new Set(entries.map((entry) => entry.key));
    for (const entry of entries) {
      const previous = renderedMarkersRef.current.get(entry.key);
      if (entry.cluster) {
        if (previous) continue;
        const clusterMarker = L.marker([entry.lat, entry.lng], {
          icon: L.divIcon({
            className: "",
            html: clusterMarkerHtml(entry.items.length),
            iconSize: [36, 36],
            iconAnchor: [18, 18],
          }),
        }).addTo(markerLayerRef.current);
        clusterMarker.bindTooltip(
          `${entry.items.length} restaurants · zoom in to explore`,
          { className: "vf-tooltip", direction: "top" }
        );
        clusterMarker.on("click", () => {
          const nextCenter = [entry.lat, entry.lng];
          const nextZoom = Math.min(
            MAP_INDIVIDUAL_MARKER_ZOOM,
            map.getZoom() + 2
          );
          if (isDesktop) map.flyTo(nextCenter, nextZoom, { duration: 0.6 });
          else map.setView(nextCenter, nextZoom, { animate: false });
        });
        renderedMarkersRef.current.set(entry.key, {
          marker: clusterMarker,
          signature: entry.key,
        });
        continue;
      }

      const baseItem = entry.items[0];
      const openStatus = restaurantOpenStates.get(baseItem.id);
      const item = {
        ...baseItem,
        openState: openStatus?.openState ?? null,
        todayHours: openStatus?.todayHours ?? null,
      };
      const signature = JSON.stringify([
        item.name,
        item.count,
        item.rating,
        item.openState,
        item.todayHours,
        item.distance,
        item.primaryType,
        item.priceLevel,
        item.websiteUrl,
      ]);
      if (previous?.signature === signature) {
        markersRef.current[item.id] = previous.marker;
        continue;
      }
      if (previous) markerLayerRef.current.removeLayer(previous.marker);
      const kind = venueKind(item.primaryType);
      const icon = L.divIcon({
        className: "",
        html: venueMarkerHtml({
          kind,
          count: item.count,
          color: "#047857",
        }),
        iconSize: VENUE_MARKER_SIZE,
        iconAnchor: VENUE_MARKER_ANCHOR,
      });
      const marker = L.marker([item.lat, item.lng], { icon }).addTo(
        markerLayerRef.current
      );
      markersRef.current[item.id] = marker;
      marker.on("click", () => {
        if (isDesktop) {
          focusMapOnMarker(map, marker);
        } else {
          map.setView(marker.getLatLng(), placeFocusZoom(map.getZoom()), {
            animate: false,
          });
          window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
              markersRef.current[item.id]?.openPopup();
            });
          });
        }
      });

      const tip = document.createElement("span");
      tip.textContent =
        `${item.name} · ${venueKindLabel(kind)} · ${item.count} matching item` +
        (item.count === 1 ? "" : "s");
      marker.bindTooltip(tip, {
        className: "vf-tooltip",
        direction: "top",
        offset: [0, -12],
      });

      // Leaflet invokes this factory only when the marker is opened. Hundreds
      // of popup DOM trees no longer sit in memory before anyone taps a pin.
      marker.bindPopup(
        () =>
          buildDishMapPopup(
            item,
            originLabel,
            () => dishMapActionsRef.current?.(item.id),
            () => {
              setMenuCommentTarget(null);
              setMenuRestaurant(item.restaurant);
            }
          ),
        { closeButton: false }
      );
      renderedMarkersRef.current.set(entry.key, { marker, signature });
    }

    for (const [key, rendered] of renderedMarkersRef.current) {
      if (activeMarkerKeys.has(key)) continue;
      markerLayerRef.current.removeLayer(rendered.marker);
      renderedMarkersRef.current.delete(key);
    }

    const originKey = `${origin.lat}:${origin.lng}:${originLabel}`;
    if (
      originKey !== lastMapOriginRef.current &&
      originLabel !== DEFAULT_ORIGIN_LABEL
    ) {
      // A chosen origin (address search or near-me) wins: center the map
      // there so it answers "what's around this spot", even when that spot
      // is far from every pin.
      map.setView([origin.lat, origin.lng], 13);
      didInitialMapFitRef.current = true;
    } else if (!didInitialMapFitRef.current && bounds.length > 0) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
      didInitialMapFitRef.current = true;
    } else {
      if (!didInitialMapFitRef.current) {
        map.setView([ORLANDO.lat, ORLANDO.lng], 10);
      }
    }
    lastMapOriginRef.current = originKey;

    const syncBounds = () => {
      const current = map.getBounds();
      const next = {
        s: current.getSouth(),
        w: current.getWest(),
        n: current.getNorth(),
        e: current.getEast(),
      };
      setMapBounds((previous) =>
        previous &&
        previous.s === next.s &&
        previous.w === next.w &&
        previous.n === next.n &&
        previous.e === next.e
          ? previous
          : next
      );
    };
    if (boundsSyncRef.current) map.off("moveend", boundsSyncRef.current);
    boundsSyncRef.current = syncBounds;
    map.on("moveend", syncBounds);
    const resizeTimer = setTimeout(() => {
      map.invalidateSize();
      syncBounds();
    }, 100);
    return () => {
      clearTimeout(resizeTimer);
      map.off("moveend", syncBounds);
    };
  }, [
    mappedRestaurants,
    showMap,
    origin,
    originLabel,
    mapZoom,
    mapBounds,
    restaurantOpenStates,
    focusedRestaurantId,
    isDesktop,
  ]);

  useEffect(
    () => () => {
      mapRef.current?.remove();
      mapRef.current = null;
      markerLayerRef.current = null;
      originMarkerRef.current = null;
      renderedMarkersRef.current.clear();
      markersRef.current = {};
    },
    []
  );

  useEffect(() => {
    if (!showMap) return;
    if (!focus || !isFreshMapFocus(focus)) {
      if (focus) setFocus(null);
      return;
    }
    const map = mapRef.current;
    const marker = markersRef.current[focus.restaurantId];
    if (!map) return;

    let cancelled = false;
    let opened = false;
    let retryTimer = null;
    let popupTimer = null;
    let firstFrame = null;
    let secondFrame = null;

    const openFocusedPopup = (attempt = 0) => {
      if (cancelled || opened) return;
      const currentMarker = markersRef.current[focus.restaurantId];
      if (currentMarker) {
        opened = true;
        currentMarker.openPopup();
        setFocus((current) => (current === focus ? null : current));
        return;
      }
      // Marker reconciliation follows zoom/move state updates. Give React a
      // short window to mount the selected restaurant after the flight.
      if (attempt < 20) {
        retryTimer = setTimeout(() => openFocusedPopup(attempt + 1), 75);
      }
    };
    if (marker) {
      if (isDesktop) focusMapOnMarker(map, marker);
      else {
        map.setView(marker.getLatLng(), placeFocusZoom(map.getZoom()), {
          animate: false,
        });
      }
    } else {
      const lat = Number(focus.lat);
      const lng = Number(focus.lng);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
        return;
      }
      const nextCenter = [lat, lng];
      const nextZoom = placeFocusZoom(map.getZoom());
      if (isDesktop) map.flyTo(nextCenter, nextZoom, { duration: 0.8 });
      else map.setView(nextCenter, nextZoom, { animate: false });
    }
    if (isDesktop) {
      popupTimer = window.setTimeout(openFocusedPopup, 900);
    } else {
      // The instant mobile jump can open as soon as Leaflet has received its
      // two-frame resize. The retry above handles a marker still reconciling.
      firstFrame = window.requestAnimationFrame(() => {
        secondFrame = window.requestAnimationFrame(() => openFocusedPopup());
      });
    }
    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
      window.clearTimeout(popupTimer);
      if (firstFrame != null) window.cancelAnimationFrame(firstFrame);
      if (secondFrame != null) window.cancelAnimationFrame(secondFrame);
    };
  }, [focus, showMap, isDesktop]);

  function toggleExpanded(dishId) {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(dishId)) next.delete(dishId);
      else next.add(dishId);
      return next;
    });
  }

  function showRestaurantOnMap(dish) {
    if (dish.lat == null || dish.lng == null) return;
    if (!isDesktop) enterMobileMap();
    setFocus({
      restaurantId: dish.restaurant_id,
      lat: dish.lat,
      lng: dish.lng,
      timestamp: Date.now(),
    });
  }

  function showRestaurantItems(restaurantId) {
    if (restaurant === "all") sortBeforeRestaurantRef.current = sortBy;
    setRestaurant(String(restaurantId));
    setPlaceType("all");
    setCuisine("all");
    setOpenFilter("all");
    setQuery("");
    setVerdicts(new Set());
    setServingRole("all");
    setMaxMiles(0);
    // A restaurant menu still needs verdict order: vegan, likely vegan,
    // adaptable, unclear, then not vegan. Alphabetical sorting scattered
    // those groups and made the shortcut feel like the list reshuffled.
    setSortBy("recommended");
    enterMobileList();
  }

  function clearRestaurantFilter() {
    setRestaurant("all");
    const previousSort = sortBeforeRestaurantRef.current;
    sortBeforeRestaurantRef.current = null;
    // Restore the earlier list sort when the restaurant view is still using
    // its automatic Best match order. A sort chosen inside the view wins.
    setSortBy((current) =>
      current === "recommended" && previousSort ? previousSort : current
    );
  }

  function toggleRestaurantFilter(restaurantId) {
    // Clicking the chip of the already-filtered restaurant removes the filter.
    if (restaurant === String(restaurantId)) clearRestaurantFilter();
    else showRestaurantItems(restaurantId);
  }

  function openDish(dish) {
    pushDishDetailRoute(dish.id);
  }

  function noteCountForDish(dish) {
    return (
      dishMentionCounts.get(`${dish.place_id}:${dishKey(dish.name)}`) || 0
    );
  }

  function openDishComments(dish, mode = "add") {
    const targetRestaurant = restaurantById.get(dish.restaurant_id);
    if (!targetRestaurant?.place_id) return;
    if (selectedDishId != null) {
      replaceHashRoute("dishes");
      setSelectedDishId(null);
    }
    setMenuCommentTarget({ dish, mode });
    setMenuRestaurant(targetRestaurant);
  }

  function closeDish() {
    setSelectedDishId(null);
    if (isOwnedDishDetailRoute()) {
      window.history.back();
    } else {
      // A shared/direct detail URL has no safe in-app entry to return to.
      // Replace it in place so Close never sends the visitor off-site.
      replaceHashRoute("dishes");
    }
  }

  function showDishOnMap(dish) {
    // Showing the map stays in Dishes, so consume the detail URL instead of
    // navigating Back (which may legitimately return to Saved).
    replaceHashRoute("dishes");
    setSelectedDishId(null);
    showRestaurantOnMap(dish);
  }

  // One origin change path for every control (sidebar picker, mobile pin,
  // mobile address row): closest-first sort and a closed address row.
  function changeOrigin(point, label) {
    setFocus(null);
    setOrigin(point);
    setOriginLabel(label);
    // Picking a location means "what's near here" — surface the closest
    // dishes instead of leaving the previous sort.
    setSortBy("distance");
    setOriginOpen(false);
  }

  function clearFilters() {
    setQuery("");
    setPlaceType("all");
    setVerdicts(new Set());
    dessertCategoriesAutoRef.current = false;
    setCategories(new Set(["food"]));
    setServingRole("all");
    setDishFormat("all");
    setAvoid(new Set());
    setSpiceFilter("all");
    setFakeMeat("all");
    setMaxPrice(0);
    setRestaurant("all");
    setCuisine("all");
    setOpenFilter("all");
    setMaxMiles(0);
    setSortBy("recommended");
  }

  const activeFilterCount =
    Number(verdicts.size > 0) +
    Number(categoriesActive) +
    Number(servingRole !== "all") +
    Number(dishFormat !== "all") +
    Number(avoid.size > 0) +
    Number(spiceFilter !== "all") +
    Number(fakeMeat !== "all") +
    Number(maxPrice > 0) +
    Number(restaurant !== "all") +
    Number(cuisine !== "all") +
    Number(placeType !== "all") +
    Number(openFilter !== "all") +
    Number(maxMiles > 0);

  return (
    <div className={`mx-auto max-w-7xl px-4 ${embedded ? "pb-8 pt-5" : "py-8"}`}>
      {!embedded && <div className="mb-6">
        <div className="mb-2 inline-flex rounded-full bg-emerald-100 px-3 py-1 text-xs font-bold uppercase tracking-wide text-emerald-800">
          Search every menu at once
        </div>
        <h1 className="text-2xl font-extrabold tracking-tight text-stone-900 sm:text-4xl">
          What are you craving?
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-stone-500">
          Search {dishes.length.toLocaleString()} menu items from {restaurants.length} restaurants
          {veganCount > 0 && `, including ${veganCount.toLocaleString()} vegan dishes`}.
          Try a dish, ingredient, cuisine, or restaurant name.
        </p>
      </div>}

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <FilterSidebar
          open={filtersOpen}
          onToggle={() => setFiltersOpen((value) => !value)}
          activeCount={activeFilterCount}
        >
        <div className="space-y-3">
          <button
            onClick={clearFilters}
            disabled={!hasActiveFilters && !query && sortBy === "recommended"}
            className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-semibold text-stone-700 transition hover:border-rose-300 hover:text-rose-600 disabled:cursor-default disabled:opacity-40"
          >
            ↺ Reset all filters
          </button>
          <div className="space-y-2">
            <div className="space-y-2">
            <select
              value={restaurant}
              onChange={(event) => {
                const value = event.target.value;
                if (value === "all") clearRestaurantFilter();
                else showRestaurantItems(value);
              }}
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
            >
              <option value="all">All restaurants</option>
              {restaurants.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
            <select
              value={cuisine}
              onChange={(event) => setCuisine(event.target.value)}
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
              aria-label="Filter by cuisine"
            >
              <option value="all">All cuisines</option>
              {cuisines.map((label) => (
                <option key={label} value={label}>{label}</option>
              ))}
            </select>
            <select
              value={openFilter}
              onChange={(event) => setOpenFilter(event.target.value)}
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
              aria-label="Filter by current opening status"
              title="Calculated from listed weekly hours; recent Google status is used as a fallback"
            >
              <option value="all">Any open status</option>
              <option value="open">Open now</option>
              <option value="closed">Closed now</option>
            </select>
            {(categories.size === 0 || categories.has("food")) && (
              <select
                value={servingRole}
                onChange={(e) => setServingRole(e.target.value)}
                className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
                aria-label="Filter by meals or sides"
                title="Full meals vs sides and small plates; items not yet reclassified count as meals"
              >
                <option value="all">Meals & sides</option>
                <option value="meal">Meals only</option>
                <option value="side">Sides & small plates</option>
              </select>
            )}
            {dishFormats.length > 0 && (
              <select
                value={dishFormat}
                onChange={(event) => setDishFormat(event.target.value)}
                className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm capitalize"
                aria-label="Filter by dish type"
                title="What the dish is like to eat — bowls, burritos, sushi, soups…"
              >
                <option value="all">Any dish type</option>
                {dishFormats.map(([format, count]) => (
                  <option key={format} value={format}>
                    {formatLabel(format)} ({count.toLocaleString()})
                  </option>
                ))}
              </select>
            )}
            <select
              value={spiceFilter}
              onChange={(event) => setSpiceFilter(event.target.value)}
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
              aria-label="Filter by spice level"
              title="From heat markers and dish names on the menu; unmarked dishes only match 'Any'"
            >
              <option value="all">Any spice level</option>
              <option value="none">No heat</option>
              <option value="any_heat">Spicy 🌶</option>
              <option value="hot">Extra hot 🌶🌶</option>
            </select>
            <select
              value={fakeMeat}
              onChange={(event) => setFakeMeat(event.target.value)}
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
              aria-label="Filter by plant-based meat"
              title="Explicit meat substitutes: Impossible, Beyond, plant-based chick'n…"
            >
              <option value="all">Plant-based meat: any</option>
              <option value="only">Only plant-based meat</option>
              <option value="exclude">No plant-based meat</option>
            </select>
            <select
              value={maxPrice}
              onChange={(e) => setMaxPrice(Number(e.target.value))}
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
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
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
            >
              <option value="recommended">Sort: Best match</option>
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
              className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
            >
              {RANGES.map((range) => (
                <option key={range.miles} value={range.miles}>{range.label}</option>
              ))}
            </select>
            <LocationPicker originLabel={originLabel} onOrigin={changeOrigin} />
            </div>
          </div>
          {/* Sidebar is narrow, so both pill groups use fixed grids instead
              of wrapping rows — even cells read as one control, not débris. */}
          <div>
            <div className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-stone-400">
              Type · pick any combination
            </div>
            <div className="grid grid-cols-3 gap-1 rounded-xl border border-stone-200 bg-stone-50 p-1">
              {CATEGORIES.map((item) => {
                const count = categoryCounts[item.key];
                // Empty set means "everything" — show all three as on.
                const active =
                  categories.size === 0 || categories.has(item.key);
                return (
                  <button
                    key={item.key}
                    onClick={() => toggleCategory(item.key)}
                    aria-pressed={active}
                    className={`flex flex-col items-center rounded-lg px-1 py-1.5 text-xs font-bold transition ${
                      active
                        ? "bg-stone-800 text-white shadow-sm"
                        : "text-stone-600 hover:bg-white"
                    }`}
                  >
                    {item.label}
                    <span className={`text-[10px] font-semibold ${active ? "text-stone-300" : "text-stone-400"}`}>
                      {count.toLocaleString()}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <div className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-stone-400">
              Verdict · pick any combination
            </div>
            <div className="grid grid-cols-2 gap-1">
              {VERDICTS.map((item) => {
                const active =
                  item.key === "all"
                    ? verdicts.size === 0
                    : verdicts.has(item.key);
                return (
                  <button
                    key={item.key}
                    onClick={() => toggleVerdict(item.key)}
                    aria-pressed={active}
                    className={`rounded-lg px-2 py-1.5 text-xs font-semibold transition ${
                      active
                        ? "bg-emerald-700 text-white shadow-sm"
                        : "border border-stone-200 bg-white text-stone-600 hover:border-emerald-500"
                    }`}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <div className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-stone-400">
              Avoid · confirmed-free only
            </div>
            <div className="grid grid-cols-3 gap-1 rounded-xl border border-stone-200 bg-stone-50 p-1">
              {AVOID_OPTIONS.map(([field, label]) => {
                const active = avoid.has(field);
                return (
                  <button
                    key={field}
                    onClick={() =>
                      setAvoid((prev) => {
                        const next = new Set(prev);
                        if (next.has(field)) next.delete(field);
                        else next.add(field);
                        return next;
                      })
                    }
                    aria-pressed={active}
                    title={`Show only dishes whose ingredients appear ${label.toLowerCase()}-free — always confirm allergies with the restaurant`}
                    className={`rounded-lg px-1 py-1.5 text-xs font-bold transition ${
                      active
                        ? "bg-rose-700 text-white shadow-sm"
                        : "text-stone-600 hover:bg-white"
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
            {avoid.size > 0 && (
              <p className="mt-1.5 text-[11px] leading-snug text-stone-400">
                Menu-based inference, not an allergy guarantee — confirm
                cross-contact with the restaurant.
              </p>
            )}
          </div>
        </div>
      </FilterSidebar>

      <div className="min-w-0 flex-1">
      <div className="mb-4 flex items-center gap-2">
      <div className="relative min-w-0 flex-1">
        <span className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-xl text-stone-400" aria-hidden="true">⌕</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search dishes, ingredients, cuisines, or restaurants…"
          className="w-full rounded-2xl border border-stone-300 bg-white py-3 pl-12 pr-11 text-base shadow-sm outline-none placeholder:text-stone-400 focus:border-emerald-600 focus:ring-2 focus:ring-emerald-100"
          aria-label="Search all menu items"
        />
        {query && (
          <button
            type="button"
            onClick={() => setQuery("")}
            className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full px-2 py-1 text-lg leading-none text-stone-400 hover:bg-stone-100 hover:text-stone-700"
            aria-label="Clear search"
          >
            ×
          </button>
        )}
      </div>
      {/* Phones: near-me one tap from the search bar — the full picker
          lives behind the collapsed filter sidebar. */}
      <NearMeIconButton className="lg:hidden" onOrigin={changeOrigin} />
      </div>

      {/* Phones: origin status + inline address search, one small line —
          no digging into the collapsed filters for the LocationPicker. */}
      <div className="-mt-2 mb-3 lg:hidden">
        <button
          type="button"
          onClick={() => setOriginOpen((value) => !value)}
          aria-expanded={originOpen}
          className="text-xs text-stone-500"
        >
          📍 Distances from{" "}
          <span className="font-semibold text-stone-700">{originLabel}</span>{" "}
          · <span className="font-semibold text-emerald-700 underline underline-offset-2">
            {originOpen ? "close" : "change"}
          </span>
        </button>
        {originOpen && (
          <div className="mt-2">
            <LocationPicker compact originLabel={originLabel} onOrigin={changeOrigin} />
          </div>
        )}
      </div>

      {/* Active filters — front and center above the results, never buried
          in the sidebar: every applied filter is a removable chip. */}
      {hasActiveFilters && (
        <div
          className="mb-4 flex flex-wrap items-center gap-2 rounded-xl border-2 border-emerald-300 bg-emerald-50 px-3 py-2.5 shadow-sm"
          aria-live="polite"
        >
          <span className="text-xs font-bold uppercase tracking-wide text-emerald-800">
            Active filters
          </span>
          {selectedRestaurant && (
            <button
              onClick={clearRestaurantFilter}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove restaurant filter"
            >
              Restaurant: {selectedRestaurant.name}
              <span aria-hidden="true" className="text-base leading-none">×</span>
            </button>
          )}
          {verdicts.size > 0 && (
            <button
              onClick={() => setVerdicts(new Set())}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove verdict filters"
            >
              Verdict:{" "}
              {VERDICTS.filter((item) => verdicts.has(item.key))
                .map((item) => item.label)
                .join(" + ")}
              <span aria-hidden="true" className="text-base leading-none">×</span>
            </button>
          )}
          {categoriesActive && (
            <button
              onClick={() => setCategories(new Set(["food"]))}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Back to food only"
            >
              Type:{" "}
              {categories.size === 0
                ? "Everything"
                : CATEGORIES.filter((item) => categories.has(item.key))
                    .map((item) => item.label)
                    .join(" + ")}
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
          {dishFormat !== "all" && (
            <button
              onClick={() => setDishFormat("all")}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove dish-type filter"
            >
              Type: <span className="capitalize">{formatLabel(dishFormat)}</span>
              <span aria-hidden="true" className="text-base leading-none">×</span>
            </button>
          )}
          {avoid.size > 0 && (
            <button
              onClick={() => setAvoid(new Set())}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove avoid filters"
            >
              Avoiding:{" "}
              {AVOID_OPTIONS.filter(([field]) => avoid.has(field))
                .map(([, label]) => label)
                .join(" + ")}
              <span aria-hidden="true" className="text-base leading-none">×</span>
            </button>
          )}
          {spiceFilter !== "all" && (
            <button
              onClick={() => setSpiceFilter("all")}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove spice filter"
            >
              {spiceFilter === "none"
                ? "No heat"
                : spiceFilter === "hot"
                  ? "Extra hot 🌶🌶"
                  : "Spicy 🌶"}
              <span aria-hidden="true" className="text-base leading-none">×</span>
            </button>
          )}
          {fakeMeat !== "all" && (
            <button
              onClick={() => setFakeMeat("all")}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove plant-based meat filter"
            >
              {fakeMeat === "only" ? "Only plant-based meat" : "No plant-based meat"}
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
          {placeType !== "all" && (
            <button
              onClick={() => changeVenueKind("all")}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove place-type filter"
            >
              Place: {venueKindLabel(placeType)}
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
          {openFilter !== "all" && (
            <button
              onClick={() => setOpenFilter("all")}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-emerald-800 shadow-sm ring-1 ring-emerald-200 hover:bg-emerald-100"
              title="Remove opening-status filter"
            >
              Status: {openFilter === "open" ? "Open now" : "Closed now"}
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

      {/* Floating view flip (phones/tablets) — thumb-reachable and
          unmissable; desktop shows both panes so it doesn't render. */}
      <div ref={mobileMapAnchorRef} className="scroll-mt-32">
        <VenueKindFilter value={placeType} onChange={changeVenueKind} />
      </div>

      <button
        type="button"
        aria-label={
          mobileView === "list" ? "Show dishes on the map" : "Show dish list"
        }
        onClick={() =>
          mobileView === "list" ? enterMobileMap() : enterMobileList()
        }
        className="fixed inset-x-0 z-40 mx-auto w-fit rounded-full bg-stone-900 px-5 py-2.5 text-sm font-bold text-white shadow-xl xl:hidden"
        style={{
          bottom: "calc(1.25rem + env(safe-area-inset-bottom, 0px))",
        }}
      >
        {mobileView === "list" ? "🗺 Map" : "☰ List"}
      </button>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}
      {directoryError && (
        <div className="mb-3 flex items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <span>
            Restaurant details did not load, so some names and map links may be unavailable.
          </span>
          <button
            type="button"
            onClick={() => setDirectoryRetry((value) => value + 1)}
            className="shrink-0 font-bold underline underline-offset-2"
          >
            Retry
          </button>
        </div>
      )}

      <div className="xl:grid xl:grid-cols-2 xl:items-start xl:gap-5">
        <div className={!isDesktop && mobileView === "map" ? "hidden" : ""}>
          {!isDesktop && mobileView === "map" ? null :
          loading ||
          (compactCatalogNeedsDirectory &&
            !restaurantContextReady &&
            !directoryError) ? (
            <div className="p-12 text-center text-stone-400">Loading the menu database…</div>
          ) : compactCatalogNeedsDirectory && !restaurantContextReady ? (
            <div className="rounded-2xl border border-dashed border-amber-300 bg-amber-50 p-10 text-center text-sm text-amber-800">
              Restaurant details are required to display this compact dish catalog. Retry the directory above.
            </div>
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
            <span>
              {query !== deferredQuery
                ? "Updating results…"
                : `${shown.length.toLocaleString()} menu item${shown.length === 1 ? "" : "s"}`}
            </span>
            {(query || hasActiveFilters) && (
              <button onClick={clearFilters} className="normal-case tracking-normal text-emerald-700 hover:underline">
                Clear filters
              </button>
            )}
          </div>
          <ol className="overflow-hidden rounded-2xl border border-stone-200 bg-white shadow-sm divide-y divide-stone-100">
            {visibleDishes.map((dish, index) => {
              const details = splitReasoning(dish.reasoning);
              const isExpanded = expandedIds.has(dish.id);
              const isSide = dish.serving_role === "side";
              const previousWasSide = visibleDishes[index - 1]?.serving_role === "side";
              // Role headings only when the list is actually role-grouped:
              // search results are ranked by relevance, where a heading at
              // every meal/side flip would repeat down the list.
              const showRoleHeading =
                foodOnly &&
                servingRole === "all" &&
                !deferredQuery.trim() &&
                (index === 0 || isSide !== previousWasSide);
              return (
                <Fragment key={dish.id}>
                {showRoleHeading && (
                  <li className="bg-stone-50 px-4 py-2 text-xs font-extrabold uppercase tracking-wider text-stone-500 sm:px-5">
                    {isSide ? "Sides & small plates" : "Meals"}
                  </li>
                )}
                <li className="transition hover:bg-stone-50">
                  {/* Compact row: name + price, then restaurant · rating ·
                      open state — the at-a-glance facts. Tap to expand. */}
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => toggleExpanded(dish.id)}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget) return;
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        toggleExpanded(dish.id);
                      }
                    }}
                    aria-expanded={isExpanded}
                    className="flex w-full cursor-pointer items-center gap-3 px-4 py-2 text-left sm:px-5"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className="truncate text-sm font-bold leading-snug text-stone-900">
                          {dish.name}
                        </span>
                        {dish.price && (
                          <span className="shrink-0 text-xs font-medium text-stone-400">
                            {dish.price}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-stone-500">
                        {dish.lat != null && dish.lng != null ? (
                          // Jumps the map to this restaurant's pin — no
                          // filters applied (the expanded chip does that).
                          <button
                            onClick={(event) => {
                              event.stopPropagation();
                              showRestaurantOnMap(dish);
                            }}
                            title={`Show ${dish.restaurant_name} on the map`}
                            className="inline-flex min-w-0 items-center gap-1 font-semibold text-emerald-700 underline decoration-emerald-300 decoration-dotted underline-offset-2 transition hover:decoration-emerald-700"
                          >
                            <svg
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2"
                              className="h-3 w-3 shrink-0"
                              aria-hidden="true"
                            >
                              <path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z" />
                              <circle cx="12" cy="10" r="2.5" />
                            </svg>
                            <span className="truncate">{dish.restaurant_name}</span>
                          </button>
                        ) : (
                          <span className="truncate">{dish.restaurant_name}</span>
                        )}
                        {dish.rating != null && (
                          <span className="shrink-0" title="Google rating">
                            · <span className="text-amber-500">★</span>{" "}
                            {Number(dish.rating).toFixed(1)}
                          </span>
                        )}
                        {(() => {
                          const openState = restaurantOpenStates.get(
                            dish.restaurant_id
                          )?.openState;
                          if (openState == null) return null;
                          return (
                            <span
                              className={`shrink-0 font-semibold ${
                                openState ? "text-emerald-700" : "text-rose-600"
                              }`}
                            >
                              · {openState ? "Open" : "Closed"}
                            </span>
                          );
                        })()}
                      </div>
                    </div>
                    <DishCommentBadge
                      count={noteCountForDish(dish)}
                      dishName={dish.name}
                      onClick={(event) => {
                        event.stopPropagation();
                        openDishComments(dish, "view");
                      }}
                    />
                    <VerdictChip verdict={dish.verdict} />
                    <span
                      aria-hidden="true"
                      className={`shrink-0 text-stone-400 transition-transform ${
                        isExpanded ? "rotate-90" : ""
                      }`}
                    >
                      ›
                    </span>
                  </div>

                  {isExpanded && (
                    <div className="border-t border-stone-100 bg-stone-50/60 px-4 py-3 sm:px-5 sm:py-4">
                      <div className="flex items-start justify-between gap-4">
                        <div className="min-w-0 flex-1">
                          {dish.raw_description && (
                            <p className="text-sm leading-relaxed text-stone-700">
                              {dish.raw_description}
                            </p>
                          )}
                          <div className="mt-2 flex flex-wrap items-center gap-1.5">
                            {dish.calories && (
                              <span className="rounded-full bg-white px-2.5 py-1 text-xs font-semibold text-stone-500 ring-1 ring-stone-200">
                                {calorieLabel(dish.calories)}
                              </span>
                            )}
                            <DietaryBadges dish={dish} maxBadges={4} />
                          </div>
                          {dish.vegan_adaptation && (
                            <p className="mt-2 text-xs font-semibold text-sky-700">
                              Make it vegan: {dish.vegan_adaptation}
                            </p>
                          )}
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          <ThumbVote
                            dishId={dish.id}
                            upVotes={dish.up_votes}
                            downVotes={dish.down_votes}
                          />
                          <FavoriteButton
                            active={favorites.dishes.includes(dish.id)}
                            onClick={() => toggleDish(dish.id)}
                            label="dish"
                          />
                        </div>
                      </div>

                      {(details.reasoning || details.evidence) && (
                        <div className="mt-3 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2.5">
                          <div className="flex flex-wrap items-baseline justify-between gap-2">
                            <span className="text-[11px] font-extrabold uppercase tracking-wide text-emerald-800">
                              Why this verdict
                            </span>
                            <span className="flex items-center gap-2">
                              {dish.confidence != null && (
                                <span className="text-[11px] font-semibold tabular-nums text-emerald-700">
                                  {Math.round(dish.confidence * 100)}% confidence
                                </span>
                              )}
                              {dish.menu_url?.startsWith("http") && (
                                <a
                                  href={dish.menu_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-[11px] font-bold text-emerald-800 underline decoration-emerald-400 underline-offset-2 hover:text-emerald-950"
                                >
                                  Source menu ↗
                                </a>
                              )}
                            </span>
                          </div>
                          {details.reasoning && (
                            <p className="mt-1 text-xs leading-relaxed text-emerald-950">
                              {details.reasoning}
                            </p>
                          )}
                          {details.evidence && (
                            <blockquote className="mt-1.5 border-l-2 border-emerald-400 pl-2.5 text-xs italic leading-relaxed text-emerald-900">
                              {details.evidence}
                            </blockquote>
                          )}
                        </div>
                      )}

                      <div className="mt-3 border-t border-stone-200 pt-3">
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-stone-500">
                          <FreshnessBadge fetchedAt={dish.menu_fetched_at} compact />
                          {dish.distance != null && dish.lat != null && dish.lng != null && (
                            <button
                              onClick={() => showRestaurantOnMap(dish)}
                              className="font-semibold text-stone-600 hover:text-emerald-700 hover:underline"
                            >
                              {dish.distance.toFixed(1)} mi from {originLabel} · map
                            </button>
                          )}
                        </div>
                        <div className="mt-2.5 flex items-center justify-between gap-2">
                          <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
                          {CLOUD_ENABLED && dish.place_id && (
                            <button
                              type="button"
                              onClick={() => openDishComments(dish, "add")}
                              className="rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1.5 font-bold text-sky-700 transition hover:border-sky-400 hover:bg-sky-100"
                            >
                              Add a note
                            </button>
                          )}
                          <button
                            onClick={() => toggleRestaurantFilter(dish.restaurant_id)}
                            className="rounded-full border border-stone-200 bg-white px-2.5 py-1.5 font-semibold text-stone-600 transition hover:border-emerald-300 hover:text-emerald-700"
                            title={
                              restaurant === String(dish.restaurant_id)
                                ? "Remove the restaurant filter"
                                : `Show every menu item from ${dish.restaurant_name}`
                            }
                          >
                            {restaurant === String(dish.restaurant_id)
                              ? "Clear filter"
                              : "All dishes"}
                          </button>
                          </div>
                          <button
                            onClick={() => openDish(dish)}
                            className="shrink-0 rounded-full bg-emerald-700 px-3 py-1.5 text-xs font-bold text-white transition hover:bg-emerald-800"
                          >
                            Full details →
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </li>
                </Fragment>
              );
            })}
          </ol>
          {visibleDishes.length < shown.length && (
            <div
              ref={loadMoreRef}
              className="py-4 text-center text-xs font-medium text-stone-400"
            >
              Showing {visibleDishes.length.toLocaleString()} of {shown.length.toLocaleString()} — loading more as you scroll…
            </div>
          )}
            </>
          )}
        </div>

        <div className={`${!isDesktop && mobileView === "list" ? "hidden" : ""} xl:sticky xl:top-32`}>
          <div className="relative z-0 isolate">
            <div
              ref={mapEl}
              className="h-[calc(100dvh-12.75rem)] min-h-[17rem] w-full overflow-hidden rounded-2xl border border-stone-200 shadow-sm xl:h-[calc(100vh-11rem)] xl:min-h-0"
            />
            <MapLegend isDesktop={isDesktop}>
              {mapZoom < MAP_INDIVIDUAL_MARKER_ZOOM ? (
                <div className="text-[11px] font-semibold text-stone-600">
                  Cluster number = nearby places
                </div>
              ) : (
                <>
                  <div className="text-[10px] font-extrabold uppercase tracking-wide text-stone-400">
                    Place type
                  </div>
                  <VenueTypeLegend />
                  <div className="mt-2 border-t border-stone-100 pt-1.5 text-[11px] font-semibold text-stone-600">
                    Number = matching menu items
                  </div>
                </>
              )}
            </MapLegend>
          </div>
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
          onAddComment={() => openDishComments(selectedDish, "add")}
          onViewComments={() => openDishComments(selectedDish, "view")}
          commentCount={noteCountForDish(selectedDish)}
        />
      )}

      {menuRestaurant && (
        <DishModal
          restaurant={menuRestaurant}
          onClose={() => {
            setMenuRestaurant(null);
            setMenuCommentTarget(null);
          }}
          onOpenDish={openDish}
          initialTab={menuCommentTarget ? "comments" : null}
          initialMention={
            menuCommentTarget?.mode === "add" ? menuCommentTarget.dish : null
          }
          initialCommentFilter={
            menuCommentTarget?.mode === "view" ? menuCommentTarget.dish : null
          }
        />
      )}
    </div>
  );
}
