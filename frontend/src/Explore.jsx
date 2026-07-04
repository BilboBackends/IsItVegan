import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import DishModal from "./DishModal.jsx";
import FavoriteButton from "./FavoriteButton.jsx";
import RatingBadge, { ratingText } from "./RatingBadge.jsx";
import {
  FreshnessBadge,
  OpenStatusBadge,
  relativeDate,
} from "./RestaurantMeta.jsx";
import { cuisineLabel, cuisineOptions } from "./cuisine.js";
import { priceLevelRank, priceLevelSymbol } from "./price.js";

// Consumer-facing view: find vegan-friendly dishes near you. Search, sort,
// distance filter, and a map (Leaflet + CARTO light tiles — keyless, so
// nothing secret ever reaches the browser, per CLAUDE.md). Desktop shows
// list and map side by side; mobile toggles between them.

const MAITLAND = { lat: 28.6278, lng: -81.3631 };

function haversineMiles(a, b) {
  const R = 3958.8;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLng = ((b.lng - a.lng) * Math.PI) / 180;
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((a.lat * Math.PI) / 180) *
      Math.cos((b.lat * Math.PI) / 180) *
      Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

function prettyType(t) {
  if (!t) return null;
  const s = t.replaceAll("_", " ").replace(/\brestaurant\b/g, "").trim();
  return s || null;
}

function pinColor(r) {
  if (!r.dish_count) return "#a8a29e"; // not analyzed yet
  if ((r.vegan_options || 0) >= 3) return "#047857";
  if ((r.vegan_options || 0) >= 1) return "#65a30d";
  return "#78716c"; // analyzed, nothing vegan
}

const SORTS = [
  { key: "vegan", label: "Most vegan meals" },
  { key: "rating", label: "Top rated" },
  { key: "distance", label: "Closest" },
  { key: "price", label: "Cheapest" },
  { key: "name", label: "Name A–Z" },
];

// Max Google price tier to show; 0 = any. Only three tiers exist locally.
const PRICE_TIERS = [
  { tier: 0, label: "Any price" },
  { tier: 1, label: "$ inexpensive" },
  { tier: 2, label: "$$ or less" },
  { tier: 3, label: "$$$ or less" },
];

const RANGES = [
  { miles: 0, label: "Any distance" },
  { miles: 1, label: "Within 1 mi" },
  { miles: 2, label: "Within 2 mi" },
  { miles: 5, label: "Within 5 mi" },
  { miles: 10, label: "Within 10 mi" },
];

const LEGEND = [
  { color: "#047857", label: "3+ vegan meals" },
  { color: "#65a30d", label: "1–2 vegan meals" },
  { color: "#78716c", label: "No vegan meals found" },
  { color: "#a8a29e", label: "Menu not analyzed" },
];

export default function Explore({
  embedded = false,
  favorites = { restaurants: [] },
  toggleRestaurant = () => {},
}) {
  const [restaurants, setRestaurants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [cuisine, setCuisine] = useState("all");
  const [sortBy, setSortBy] = useState("vegan");
  const [priceTier, setPriceTier] = useState(0);
  const [maxMiles, setMaxMiles] = useState(0);
  const [origin, setOrigin] = useState(MAITLAND);
  const [originLabel, setOriginLabel] = useState("Maitland");
  const [locating, setLocating] = useState(false);
  const [view, setView] = useState("list"); // mobile toggle; desktop shows both
  const [isDesktop, setIsDesktop] = useState(
    () => window.matchMedia("(min-width: 1024px)").matches
  );
  const [dishesFor, setDishesFor] = useState(null);
  // {id, ts, source: "card" | "map"} — card click flies the map; pin click
  // highlights + scrolls the card. Only card-sourced focus moves the map.
  const [focus, setFocus] = useState(null);
  const [viewBounds, setViewBounds] = useState(null); // map viewport {s,w,n,e}
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef({});

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const onChange = (e) => setIsDesktop(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    fetch("/api/restaurants")
      .then((res) => {
        if (!res.ok) throw new Error(`API ${res.status}`);
        return res.json();
      })
      .then((data) => setRestaurants(data.restaurants))
      .catch((e) => setError(e.message || "Backend not reachable on :5000"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const match = window.location.hash.match(/^#restaurants\?restaurant=(\d+)/);
    if (!match || restaurants.length === 0) return;
    const target = restaurants.find((item) => item.id === Number(match[1]));
    if (!target) return;
    if (!isDesktop) setView("map");
    setFocus({ id: target.place_id, ts: Date.now(), source: "card" });
  }, [restaurants, isDesktop]);

  function useMyLocation() {
    if (!navigator.geolocation) return;
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setOrigin({ lat: pos.coords.latitude, lng: pos.coords.longitude });
        setOriginLabel("your location");
        setLocating(false);
      },
      () => setLocating(false),
      { timeout: 8000 }
    );
  }

  const enriched = useMemo(
    () =>
      restaurants.map((r) => ({
        ...r,
        distance:
          r.lat != null && r.lng != null
            ? haversineMiles(origin, { lat: r.lat, lng: r.lng })
            : null,
      })),
    [restaurants, origin]
  );

  const cuisines = useMemo(() => cuisineOptions(restaurants), [restaurants]);

  const filtered = useMemo(() => {
    let out = enriched;
    const q = query.trim().toLowerCase();
    if (q) {
      out = out.filter(
        (r) =>
          r.name?.toLowerCase().includes(q) ||
          (prettyType(r.primary_type) || "").includes(q) ||
          r.address?.toLowerCase().includes(q)
      );
    }
    if (cuisine !== "all") {
      out = out.filter((restaurant) => cuisineLabel(restaurant.primary_type) === cuisine);
    }
    if (maxMiles > 0) {
      out = out.filter((r) => r.distance != null && r.distance <= maxMiles);
    }
    if (priceTier > 0) {
      // Only restaurants Google has priced can honestly match a price cap.
      out = out.filter((r) => {
        const rank = priceLevelRank(r.price_level);
        return rank != null && rank <= priceTier;
      });
    }
    return [...out].sort((a, b) => {
      if (sortBy === "vegan") {
        if ((b.vegan_options || 0) !== (a.vegan_options || 0))
          return (b.vegan_options || 0) - (a.vegan_options || 0);
        return (a.distance ?? 1e9) - (b.distance ?? 1e9);
      }
      if (sortBy === "price") {
        return (
          (priceLevelRank(a.price_level) ?? 9) -
            (priceLevelRank(b.price_level) ?? 9) ||
          (b.vegan_options || 0) - (a.vegan_options || 0)
        );
      }
      if (sortBy === "distance") return (a.distance ?? 1e9) - (b.distance ?? 1e9);
      if (sortBy === "rating") {
        return (
          (b.rating ?? -1) - (a.rating ?? -1) ||
          (b.user_rating_count ?? 0) - (a.user_rating_count ?? 0)
        );
      }
      return (a.name || "").localeCompare(b.name || "");
    });
  }, [enriched, query, cuisine, maxMiles, sortBy, priceTier]);

  const showMap = isDesktop || view === "map";

  // (Re)build the map whenever it's visible and inputs change. Cheap at this
  // scale (~60 markers), and much simpler than incremental marker sync.
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

    // Origin marker (small blue dot).
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

    filtered.forEach((r) => {
      if (r.lat == null || r.lng == null) return;
      const color = pinColor(r);
      const analyzed = r.dish_count > 0;
      const icon = L.divIcon({
        className: "",
        html: analyzed
          ? `<div class="vf-pin" style="background:${color}">${r.vegan_options || 0}</div>`
          : `<div class="vf-pin vf-pin--dot" style="background:${color}"></div>`,
        iconSize: analyzed ? [26, 26] : [12, 12],
        iconAnchor: analyzed ? [13, 13] : [6, 6],
      });
      const marker = L.marker([r.lat, r.lng], { icon }).addTo(map);
      markersRef.current[r.place_id] = marker;
      bounds.push([r.lat, r.lng]);
      marker.on("click", () =>
        setFocus({ id: r.place_id, ts: Date.now(), source: "map" })
      );

      // Tooltip and popup content built as DOM nodes with textContent —
      // never innerHTML with API-sourced strings.
      const tipEl = document.createElement("span");
      tipEl.textContent = r.name || "";
      marker.bindTooltip(tipEl, {
        className: "vf-tooltip",
        direction: "top",
        offset: [0, -10],
      });

      const el = document.createElement("div");
      el.style.minWidth = "180px";
      const title = document.createElement("div");
      title.style.cssText = "font-weight:700;font-size:14px";
      title.textContent = r.name || "";
      const sub = document.createElement("div");
      sub.style.cssText = "color:#57534e;font-size:12px;text-transform:capitalize";
      sub.textContent =
        (prettyType(r.primary_type) || "restaurant") +
        (priceLevelSymbol(r.price_level)
          ? ` · ${priceLevelSymbol(r.price_level)}`
          : "");
      const count = document.createElement("div");
      count.style.cssText = `margin-top:4px;font-size:13px;font-weight:600;color:${
        (r.vegan_options || 0) > 0 ? "#047857" : "#57534e"
      }`;
      count.textContent = analyzed
        ? `${r.vegan_options || 0} vegan meal${r.vegan_options === 1 ? "" : "s"}` +
          ((r.vegan_sides || 0) > 0
            ? ` · ${r.vegan_sides} side${r.vegan_sides === 1 ? "" : "s"}`
            : "")
        : "Menu not analyzed yet";
      el.append(title, sub, count);
      const googleRating = ratingText(r.rating, r.user_rating_count);
      if (googleRating) {
        const rating = document.createElement("div");
        rating.style.cssText =
          "margin-top:4px;color:#78716c;font-size:12px;font-weight:600";
        rating.textContent = `${googleRating} Google`;
        el.append(rating);
      }
      const hoursFresh =
        r.enriched_at && Date.now() - new Date(r.enriched_at).getTime() < 86_400_000;
      if (r.open_now != null && hoursFresh) {
        const status = document.createElement("div");
        status.style.cssText = `margin-top:3px;font-size:12px;font-weight:700;color:${
          r.open_now ? "#047857" : "#be123c"
        }`;
        status.textContent = r.open_now ? "Open now" : "Closed";
        el.append(status);
      }
      const checked = relativeDate(r.menu_fetched_at);
      if (checked) {
        const freshness = document.createElement("div");
        freshness.style.cssText = "margin-top:3px;color:#a8a29e;font-size:11px";
        freshness.textContent = `Menu checked ${checked}`;
        el.append(freshness);
      }
      if (analyzed) {
        const btn = document.createElement("button");
        btn.textContent = "See dishes →";
        btn.style.cssText =
          "margin-top:6px;color:#047857;font-weight:700;cursor:pointer;background:none;border:none;padding:0;font-size:13px";
        btn.onclick = () => setDishesFor(r);
        el.append(btn);
      }
      marker.bindPopup(el, { closeButton: false });
    });

    if (bounds.length > 0) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
    } else {
      map.setView([origin.lat, origin.lng], 13);
    }

    // Keep the list's "On the map" section in sync with the viewport.
    const syncBounds = () => {
      const b = map.getBounds();
      setViewBounds({
        s: b.getSouth(),
        w: b.getWest(),
        n: b.getNorth(),
        e: b.getEast(),
      });
    };
    map.on("moveend", syncBounds);
    const resizeTimer = setTimeout(() => {
      map.invalidateSize();
      syncBounds();
    }, 100);
    return () => {
      clearTimeout(resizeTimer);
      map.off("moveend", syncBounds);
      map.remove();
      mapRef.current = null;
      markersRef.current = {};
    };
  }, [showMap, filtered, origin, originLabel]);

  // Card click → fly the map to that restaurant and open its popup. Runs
  // after the build effect above (declaration order), so it also works on
  // mobile where the click first flips the view to the map. Pin clicks
  // (source "map") don't move the map — they highlight + scroll the card.
  useEffect(() => {
    if (!focus) return;
    if (focus.source === "map") {
      document
        .getElementById(`vf-card-${focus.id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }
    const map = mapRef.current;
    const marker = markersRef.current[focus.id];
    if (!map || !marker) return;
    map.flyTo(marker.getLatLng(), 16, { duration: 0.8 });
    const t = setTimeout(() => marker.openPopup(), 850);
    return () => clearTimeout(t);
  }, [focus, showMap]);

  function focusRestaurant(r) {
    if (r.lat == null || r.lng == null) return;
    if (!isDesktop) setView("map");
    setFocus({ id: r.place_id, ts: Date.now(), source: "card" });
  }

  // Split the list by the map viewport: what you can see on the map sits on
  // top; everything else drops to a "Not on the map" section below (never
  // hidden). Before the map has ever reported bounds, show a single list.
  const inBounds = (r) =>
    viewBounds != null &&
    r.lat != null &&
    r.lng != null &&
    r.lat >= viewBounds.s &&
    r.lat <= viewBounds.n &&
    r.lng >= viewBounds.w &&
    r.lng <= viewBounds.e;
  const onMap = viewBounds ? filtered.filter(inBounds) : filtered;
  const offMap = viewBounds ? filtered.filter((r) => !inBounds(r)) : [];

  const renderCard = (r) => (
    <div
      key={r.place_id}
      id={`vf-card-${r.place_id}`}
      onClick={() => focusRestaurant(r)}
      title={r.lat != null ? "Show on map" : undefined}
      className={`group flex cursor-pointer flex-col rounded-2xl border bg-white p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md ${
        focus?.id === r.place_id
          ? "border-emerald-600 ring-1 ring-emerald-600"
          : "border-stone-200/80"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="font-bold leading-snug text-stone-900">{r.name}</div>
        <div className="flex shrink-0 items-center gap-2">
          {r.distance != null && (
            <span className="rounded-full bg-stone-100 px-2 py-0.5 text-xs font-medium text-stone-500">
              {r.distance.toFixed(1)} mi
            </span>
          )}
          <FavoriteButton
            active={favorites.restaurants.includes(r.id)}
            onClick={() => toggleRestaurant(r.id)}
            label="restaurant"
          />
        </div>
      </div>
      <div className="mt-0.5 text-xs capitalize text-stone-500">
        {prettyType(r.primary_type) || "restaurant"}
        {priceLevelSymbol(r.price_level) && (
          <span
            className="ml-2 font-semibold text-stone-600"
            title="Google price level"
          >
            {priceLevelSymbol(r.price_level)}
          </span>
        )}
        {r.serves_vegetarian === 1 && (
          <span className="ml-2 font-medium text-emerald-700">
            ✓ veg-friendly
          </span>
        )}
      </div>
      <RatingBadge
        rating={r.rating}
        userRatingCount={r.user_rating_count}
        className="mt-1"
      />
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <OpenStatusBadge openNow={r.open_now} enrichedAt={r.enriched_at} />
        <FreshnessBadge fetchedAt={r.menu_fetched_at} compact />
      </div>
      {r.editorial_summary && (
        <div className="mt-2 line-clamp-2 text-xs leading-relaxed text-stone-500">
          {r.editorial_summary}
        </div>
      )}
      <div className="mt-3 flex items-center justify-between border-t border-stone-100 pt-3">
        {r.dish_count > 0 ? (
          <span
            className={`rounded-full px-2.5 py-1 text-xs font-bold ${
              r.vegan_options > 0
                ? "bg-emerald-700 text-white"
                : "bg-stone-100 text-stone-500"
            }`}
          >
            {r.vegan_options} vegan meal{r.vegan_options === 1 ? "" : "s"}
            {(r.vegan_sides || 0) > 0 && ` +${r.vegan_sides} side${r.vegan_sides === 1 ? "" : "s"}`}
          </span>
        ) : (
          <span className="text-xs italic text-stone-400">
            menu not analyzed yet
          </span>
        )}
        <div className="flex items-center gap-3">
          {r.website_url && (
            <a
              href={r.website_url}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-xs font-semibold text-stone-400 transition hover:text-emerald-700 hover:underline"
            >
              Website ↗
            </a>
          )}
          {r.dish_count > 0 && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                setDishesFor(r);
              }}
              className="text-xs font-bold text-emerald-700 opacity-80 transition group-hover:opacity-100 hover:underline"
            >
              See dishes →
            </button>
          )}
        </div>
      </div>
    </div>
  );

  const analyzed = restaurants.filter((r) => r.dish_count > 0).length;
  const totalVegan = restaurants.reduce((s, r) => s + (r.vegan_options || 0), 0);
  const totalVeganSides = restaurants.reduce(
    (sum, restaurant) => sum + (restaurant.vegan_sides || 0),
    0
  );

  return (
    <div className={`mx-auto max-w-7xl px-4 ${embedded ? "pb-8 pt-5" : "py-8"}`}>
      {/* Hero */}
      {!embedded && <div className="mb-6">
        <h1 className="text-3xl font-extrabold tracking-tight text-stone-900 sm:text-4xl">
          Find <span className="text-emerald-700">vegan-friendly</span> dishes
          near you
        </h1>
        <p className="mt-2 text-sm text-stone-500">
          <span className="font-semibold text-stone-700">{totalVegan}</span>{" "}
          vegan meals
          {totalVeganSides > 0 && ` plus ${totalVeganSides} sides/small plates`} found across{" "}
          <span className="font-semibold text-stone-700">{analyzed}</span>{" "}
          analyzed menus — every verdict backed by menu evidence, even when the
          restaurant never says "vegan".
        </p>
      </div>}

      {/* Filter bar */}
      <div className="sticky top-[113px] z-10 -mx-4 mb-6 border-y border-stone-200/70 bg-[#faf8f4]/95 px-4 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search name, cuisine, address…"
            className="w-full max-w-xs rounded-full border border-stone-300 bg-white px-4 py-2 text-sm shadow-sm outline-none placeholder:text-stone-400 focus:border-emerald-600 focus:ring-1 focus:ring-emerald-600"
          />
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
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
          >
            {SORTS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
          <select
            value={maxMiles}
            onChange={(e) => setMaxMiles(Number(e.target.value))}
            className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
          >
            {RANGES.map((r) => (
              <option key={r.miles} value={r.miles}>
                {r.label}
              </option>
            ))}
          </select>
          <select
            value={priceTier}
            onChange={(e) => setPriceTier(Number(e.target.value))}
            className="rounded-full border border-stone-300 bg-white px-3 py-2 text-sm shadow-sm"
            aria-label="Filter by price level"
            title="Google price level; restaurants without one are hidden while a price filter is active"
          >
            {PRICE_TIERS.map((p) => (
              <option key={p.tier} value={p.tier}>
                {p.label}
              </option>
            ))}
          </select>
          <button
            onClick={useMyLocation}
            disabled={locating}
            className="rounded-full border border-stone-300 bg-white px-4 py-2 text-sm font-medium text-stone-700 shadow-sm hover:border-emerald-600 hover:text-emerald-700 disabled:text-stone-400"
            title={`Distances measured from ${originLabel}`}
          >
            {locating ? "Locating…" : "📍 Near me"}
          </button>
          <span className="hidden text-xs text-stone-400 sm:inline">
            from {originLabel}
          </span>
          {/* Mobile-only view toggle; desktop shows both panes */}
          <div className="ml-auto flex overflow-hidden rounded-full border border-stone-300 bg-white shadow-sm lg:hidden">
            {["list", "map"].map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-4 py-2 text-sm font-semibold capitalize ${
                  view === v ? "bg-emerald-700 text-white" : "text-stone-600"
                }`}
              >
                {v}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="lg:grid lg:grid-cols-2 lg:items-start lg:gap-5">
        {/* List pane */}
        <div className={!isDesktop && view === "map" ? "hidden" : ""}>
          {loading ? (
            <div className="p-10 text-center text-stone-400">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="p-10 text-center text-stone-400">
              No restaurants match.
            </div>
          ) : (
            <>
              <div className="mb-2 text-xs font-medium uppercase tracking-wide text-stone-400">
                {viewBounds
                  ? `🗺 On the map (${onMap.length})`
                  : `${filtered.length} restaurant${filtered.length === 1 ? "" : "s"}`}
              </div>
              {onMap.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-stone-300 p-6 text-center text-sm text-stone-400">
                  Nothing in the current map view — pan or zoom out.
                </div>
              ) : (
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
                  {onMap.map(renderCard)}
                </div>
              )}
              {offMap.length > 0 && (
                <>
                  <div className="mb-2 mt-8 text-xs font-medium uppercase tracking-wide text-stone-400">
                    Not on the map ({offMap.length})
                  </div>
                  <div className="grid gap-3 opacity-90 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
                    {offMap.map(renderCard)}
                  </div>
                </>
              )}
            </>
          )}
        </div>

        {/* Map pane — sticky on desktop, full-width on mobile map view */}
        <div
          className={`${
            !isDesktop && view === "list" ? "hidden" : ""
          } lg:sticky lg:top-32`}
        >
          <div className="relative z-0 isolate">
            <div
              ref={mapEl}
              className="h-[70vh] w-full overflow-hidden rounded-2xl border border-stone-200 shadow-sm lg:h-[calc(100vh-11rem)]"
            />
            {/* Legend */}
            <div className="pointer-events-none absolute bottom-4 left-4 z-[500] rounded-xl border border-stone-200 bg-white/95 px-3 py-2 shadow-md">
              {LEGEND.map((l) => (
                <div key={l.label} className="flex items-center gap-2 py-0.5">
                  <span
                    className="inline-block h-3 w-3 rounded-full border border-white shadow"
                    style={{ background: l.color }}
                  />
                  <span className="text-xs font-medium text-stone-600">
                    {l.label}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {dishesFor && (
        <DishModal restaurant={dishesFor} onClose={() => setDishesFor(null)} />
      )}
    </div>
  );
}
