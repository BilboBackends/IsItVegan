import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import DishDetail from "./DishDetail.jsx";
import DishModal from "./DishModal.jsx";
import FilterSidebar from "./FilterSidebar.jsx";
import LocationPicker, { NearMeIconButton } from "./LocationPicker.jsx";
import FavoriteButton from "./FavoriteButton.jsx";
import { formatRatingCount, ratingText } from "./RatingBadge.jsx";
import {
  FreshnessBadge,
  currentOpenState,
  isMenuStale,
  relativeDate,
  todayOpeningHours,
} from "./RestaurantMeta.jsx";
import { cuisineLabel, cuisineOptions, isDessertVenue } from "./cuisine.js";
import { priceLevelRank, priceLevelSymbol } from "./price.js";
import { apiUrl } from "./staticData.js";
import NoteIcon from "./NoteIcon.jsx";
import {
  CLOUD_ENABLED,
  clearCommentAuthReturn,
  dishKey,
  fetchCommentCounts,
  fetchDishMentionCounts,
  pendingCommentAuthReturn,
} from "./cloud.js";

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

function veganOptionSummary(r) {
  if (!(r.dish_count > 0)) return "Menu not analyzed";
  const unit = isDessertVenue(r.primary_type) ? "treat" : "meal";
  if (r.vegan_options > 0) {
    return (
      `${r.vegan_options} vegan ${unit}${r.vegan_options === 1 ? "" : "s"}` +
      ((r.vegan_sides || 0) > 0
        ? ` · ${r.vegan_sides} side${r.vegan_sides === 1 ? "" : "s"}`
        : "")
    );
  }
  if ((r.vegan_sides || 0) > 0) {
    return `${r.vegan_sides} vegan side${r.vegan_sides === 1 ? "" : "s"}`;
  }
  return `No vegan ${unit}s found`;
}

const SORTS = [
  { key: "score", label: "Vegan score (best)" },
  { key: "vegan", label: "Most vegan meals" },
  { key: "rating", label: "Top rated" },
  { key: "distance", label: "Closest" },
  { key: "price", label: "Cheapest" },
  { key: "name", label: "Name A–Z" },
];

// Badge color tiers for the 0-10 Vegan Score.
export function veganScoreClasses(score) {
  if (score >= 7) return "bg-emerald-600 text-white";
  if (score >= 4.5) return "bg-emerald-100 text-emerald-800";
  if (score >= 2) return "bg-amber-100 text-amber-800";
  return "bg-stone-100 text-stone-500";
}

// Inline sprout icon: strokes follow the text color, so it's white on the
// solid badge and emerald on light ones — and it renders identically on
// every platform, unlike the 🌱 emoji (which blends into the green badge
// on phones).
function SproutIcon({ className = "" }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`inline-block h-3.5 w-3.5 align-[-2px] ${className}`}
      aria-hidden="true"
    >
      <path d="M7 20h10" />
      <path d="M10 20c5.5-2.5.8-6.4 3-10" />
      <path d="M9.5 9.4c1.1.8 1.8 2.2 2.3 3.7-2 .4-3.5.4-4.8-.3-1.2-.6-2.3-1.9-3-4.2 2.8-.5 4.4 0 5.5.8z" />
      <path d="M14.1 6a7 7 0 0 0-1.1 4c1.9-.1 3.3-.6 4.3-1.4 1-1 1.6-2.3 1.7-4.6-2.7.1-4 1-4.9 2z" />
    </svg>
  );
}

function ScoreBar({ label, value, max, note }) {
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="font-semibold text-stone-700">{label}</span>
        <span className="font-bold tabular-nums text-stone-900">
          {value}
          <span className="font-normal text-stone-400">/{max}</span>
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-stone-100">
        <div
          className="h-full rounded-full bg-emerald-500"
          style={{ width: `${Math.min(100, (value / max) * 100)}%` }}
        />
      </div>
      <div className="mt-0.5 text-[11px] leading-snug text-stone-400">{note}</div>
    </div>
  );
}

// Click the score badge → a small popover shows the actual math: one bar
// per component with what earned it. Explainability as UI, not a wall of
// tooltip text. open/onToggle are controlled by the card list so the card
// with an open popover can be LIFTED above the map's stacking layer —
// otherwise the map paints over popovers on adjacent cards.
function VeganScoreBadge({ r, open, onToggle }) {
  const p = r.vegan_score_parts;
  if (r.vegan_score == null || !(r.dish_count > 0) || !p) return null;
  const treat = p.basis === "treat_variety";
  const unit = treat ? "treat" : "meal";
  const setOpen = (value) => onToggle(value ? r.place_id : null);
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(!open);
        }}
        className={`rounded-full px-2 py-0.5 text-xs font-bold ${veganScoreClasses(r.vegan_score)}`}
        title="How is this score calculated? Click to see the math"
        aria-expanded={open}
      >
        <SproutIcon /> {r.vegan_score.toFixed(1)}
      </button>
      {open && (
        <>
          <div
            className="fixed inset-0 z-40"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
            }}
          />
          <div
            className="absolute left-0 top-full z-50 mt-1.5 w-64 space-y-2.5 rounded-xl border border-stone-200 bg-white p-3 text-left shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-baseline justify-between border-b border-stone-100 pb-1.5">
              <span className="text-xs font-extrabold uppercase tracking-wide text-stone-500">
                <SproutIcon className="text-emerald-600" /> Vegan score
              </span>
              <span className="text-sm font-extrabold text-stone-900">
                {r.vegan_score.toFixed(1)}
                <span className="font-normal text-stone-400">/10</span>
              </span>
            </div>
            <ScoreBar
              label="Selection"
              value={p.selection}
              max={5}
              note={
                `${r.vegan_options} vegan ${unit}${r.vegan_options === 1 ? "" : "s"}` +
                ((r.vegan_sides || 0) > 0 ? ` + ${r.vegan_sides} side${r.vegan_sides === 1 ? "" : "s"}` : "") +
                " — each extra option counts a little less"
              }
            />
            <ScoreBar
              label="Substance"
              value={p.substance}
              max={3}
              note={
                treat
                  ? "Vegan treat variety — it's a dessert spot"
                  : "Filling options: protein-rich dishes, purpose-built vegan mains, or vegan proteins on the menu"
              }
            />
            <ScoreBar
              label="Reputation"
              value={p.reputation}
              max={2}
              note={
                r.rating != null
                  ? `${Number(r.rating).toFixed(1)}★ on Google (3.0★ → 0, 5.0★ → 2)`
                  : "No Google rating yet — scored neutral"
              }
            />
          </div>
        </>
      )}
    </span>
  );
}

function veganScoreTitle(r) {
  const p = r.vegan_score_parts;
  if (!p) return "Vegan score";
  const substanceMeaning =
    p.basis === "treat_variety"
      ? "(vegan treat variety — it's a dessert spot)"
      : "(filling vegan options: protein-rich dishes, purpose-built vegan mains, or vegan proteins offered on the menu)";
  return (
    `Vegan score ${p.score}/10 — selection ${p.selection}/5 ` +
    `(vegan options with diminishing returns), substance ${p.substance}/3 ` +
    `${substanceMeaning}, reputation ${p.reputation}/2 (Google rating)`
  );
}

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
  favorites = { restaurants: [], dishes: [] },
  toggleRestaurant = () => {},
  toggleDish = () => {},
}) {
  const [restaurants, setRestaurants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [cuisine, setCuisine] = useState("all");
  const [sortBy, setSortBy] = useState("score");
  const [priceTier, setPriceTier] = useState(0);
  const [openFilter, setOpenFilter] = useState("all");
  const [maxMiles, setMaxMiles] = useState(0);
  const [origin, setOrigin] = useState(MAITLAND);
  const [originLabel, setOriginLabel] = useState("Maitland");
  const [view, setView] = useState("list"); // mobile toggle; desktop shows both
  // Phones: filters collapse behind a disclosure (a swipe strip was
  // undiscoverable); desktop always shows them inline.
  const [filtersOpen, setFiltersOpen] = useState(
    () => window.matchMedia("(min-width: 1024px)").matches
  );
  const [isDesktop, setIsDesktop] = useState(
    () => window.matchMedia("(min-width: 1024px)").matches
  );
  const [dishesFor, setDishesFor] = useState(null);
  // "all" | "veganish" — clicking a card's vegan-count text opens the menu
  // pre-filtered to the vegan-friendly items.
  const [dishesFilter, setDishesFilter] = useState("all");
  // "comments" opens the menu modal straight on its Notes tab.
  const [dishesTab, setDishesTab] = useState(null);
  const [dishesMention, setDishesMention] = useState(null);
  const [dishesCommentFilter, setDishesCommentFilter] = useState(null);
  // place_id -> comment count for the card chips; empty map when the
  // account backend isn't configured.
  const [commentCounts, setCommentCounts] = useState(null);
  const [dishMentionCounts, setDishMentionCounts] = useState(() => new Map());

  useEffect(() => {
    if (!CLOUD_ENABLED) return;
    const refresh = () => {
      fetchCommentCounts()
        .then(setCommentCounts)
        .catch(() => {});
      fetchDishMentionCounts()
        .then(setDishMentionCounts)
        .catch(() => {});
    };
    refresh();
    window.addEventListener("dishtune:comments-changed", refresh);
    return () =>
      window.removeEventListener("dishtune:comments-changed", refresh);
  }, []);
  // place_id of the card whose score popover is open (that card gets a
  // higher z-index so the popover isn't painted under the map).
  const [scoreOpenFor, setScoreOpenFor] = useState(null);
  // Dish detail opened from the menu modal — hosted HERE so it never
  // navigates away to the Dishes tab. The dish is merged with its
  // restaurant's fields because the per-restaurant dish rows don't carry
  // restaurant context.
  const [detailDish, setDetailDish] = useState(null);
  // {id, ts, source: "card" | "map"} — card click flies the map; pin click
  // highlights + scrolls the card. Only card-sourced focus moves the map.
  const [focus, setFocus] = useState(null);
  const [viewBounds, setViewBounds] = useState(null); // map viewport {s,w,n,e}
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef({});
  const commentsReturnHandledRef = useRef(false);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const onChange = (e) => setIsDesktop(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    fetch(apiUrl("/api/restaurants"))
      .then((res) => {
        if (!res.ok) throw new Error(`API ${res.status}`);
        return res.json();
      })
      .then((data) => setRestaurants(data.restaurants))
      .catch((e) => setError(e.message || "Backend not reachable on :5000"))
      .finally(() => setLoading(false));
  }, []);

  // OAuth and email magic links return with the restaurant's stable Google
  // place id. Reopen that exact record on its comments tab, then remove the
  // temporary parameter so refreshes do not keep forcing the modal open.
  useEffect(() => {
    if (commentsReturnHandledRef.current || restaurants.length === 0) return;
    const url = new URL(window.location.href);
    const storedReturn = pendingCommentAuthReturn();
    const returnPlaceId =
      url.searchParams.get("comments") || storedReturn?.placeId;
    const linkedDishName = url.searchParams.get("note");
    const storedDishName =
      storedReturn && storedReturn.placeId === returnPlaceId
        ? storedReturn.dishName
        : null;
    if (!returnPlaceId) return;
    const target = restaurants.find(
      (restaurant) => restaurant.place_id === returnPlaceId
    );
    if (!target) {
      clearCommentAuthReturn();
      return;
    }
    commentsReturnHandledRef.current = true;
    clearCommentAuthReturn();
    setDishesFilter("all");
    setDishesTab("comments");
    setDishesMention(linkedDishName ? null : storedDishName || null);
    setDishesCommentFilter(linkedDishName || null);
    setDishesFor(target);
    url.searchParams.delete("comments");
    url.searchParams.delete("note");
    window.history.replaceState(null, "", url.toString());
  }, [restaurants]);

  useEffect(() => {
    const match = window.location.hash.match(/^#restaurants\?restaurant=(\d+)/);
    if (!match || restaurants.length === 0) return;
    const target = restaurants.find((item) => item.id === Number(match[1]));
    if (!target) return;
    if (!isDesktop) setView("map");
    setFocus({ id: target.place_id, ts: Date.now(), source: "card" });
  }, [restaurants, isDesktop]);

  function clearFilters() {
    setQuery("");
    setCuisine("all");
    setOpenFilter("all");
    setSortBy("score");
    setMaxMiles(0);
    setPriceTier(0);
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
    if (openFilter !== "all") {
      const expected = openFilter === "open";
      out = out.filter(
        (restaurant) =>
          currentOpenState(
            restaurant.open_now,
            restaurant.enriched_at,
            restaurant.opening_hours
          ) === expected
      );
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
      if (sortBy === "score") {
        if ((b.vegan_score || 0) !== (a.vegan_score || 0))
          return (b.vegan_score || 0) - (a.vegan_score || 0);
        return (a.distance ?? 1e9) - (b.distance ?? 1e9);
      }
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
  }, [enriched, query, cuisine, openFilter, maxMiles, sortBy, priceTier]);

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

      // Popup: three tidy sections (identity / status / vegan) plus an
      // actions row, separated by hairlines instead of a loose stack.
      const el = document.createElement("div");
      el.style.cssText = "min-width:200px;max-width:250px";

      const addSection = () => {
        const s = document.createElement("div");
        s.style.cssText = "padding:6px 0 5px;border-top:1px solid #e7e5e4";
        el.append(s);
        return s;
      };

      // — Identity —
      const head = document.createElement("div");
      head.style.cssText = "padding-bottom:6px";
      const title = document.createElement("div");
      title.style.cssText = "font-weight:700;font-size:14px;color:#1c1917";
      title.textContent = r.name || "";
      const sub = document.createElement("div");
      sub.style.cssText =
        "color:#78716c;font-size:12px;text-transform:capitalize;margin-top:1px";
      sub.textContent =
        (prettyType(r.primary_type) || "restaurant") +
        (priceLevelSymbol(r.price_level)
          ? ` · ${priceLevelSymbol(r.price_level)}`
          : "");
      head.append(title, sub);
      if (r.address) {
        const address = document.createElement("a");
        address.style.cssText =
          "display:block;margin-top:2px;color:#0369a1;font-size:11px;line-height:1.35;text-decoration:underline;text-underline-offset:2px";
        address.textContent = r.address;
        address.href =
          `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(r.address || r.name || "")}` +
          (r.place_id ? `&query_place_id=${encodeURIComponent(r.place_id)}` : "");
        address.target = "_blank";
        address.rel = "noopener noreferrer";
        address.title = "Open address in Google Maps";
        head.append(address);
      }
      el.append(head);

      // — Status: open state · today's hours, then rating —
      const openState = currentOpenState(r.open_now, r.enriched_at, r.opening_hours);
      const todayHours = todayOpeningHours(r.opening_hours);
      const googleRating = ratingText(r.rating, r.user_rating_count);
      if (openState != null || todayHours || googleRating) {
        const status = addSection();
        if (openState != null || todayHours) {
          const row = document.createElement("div");
          row.style.cssText = "font-size:12px;font-weight:600;color:#57534e";
          if (openState != null) {
            const state = document.createElement("span");
            state.style.cssText = `font-weight:700;color:${
              openState ? "#047857" : "#be123c"
            }`;
            state.textContent = openState ? "Open now" : "Closed";
            row.append(state);
            if (todayHours) row.append(" · ");
          }
          if (todayHours) row.append(`Today: ${todayHours}`);
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

      // — Vegan —
      const vegan = addSection();
      const count = document.createElement("div");
      count.style.cssText = `font-size:13px;font-weight:600;color:${
        (r.vegan_options || 0) > 0 ? "#047857" : "#57534e"
      }`;
      if (r.vegan_score != null && analyzed) {
        const scoreBadge = document.createElement("div");
        scoreBadge.style.cssText =
          "font-size:12px;font-weight:700;color:#047857;margin-bottom:2px";
        scoreBadge.textContent = `🌱 Vegan score ${r.vegan_score.toFixed(1)}/10`;
        scoreBadge.title = veganScoreTitle(r);
        vegan.append(scoreBadge);
      }
      const countUnit = isDessertVenue(r.primary_type) ? "treat" : "meal";
      count.textContent = analyzed
        ? `${r.vegan_options || 0} vegan ${countUnit}${r.vegan_options === 1 ? "" : "s"}` +
          ((r.vegan_sides || 0) > 0
            ? ` · ${r.vegan_sides} side${r.vegan_sides === 1 ? "" : "s"}`
            : "")
        : "Menu not analyzed yet";
      vegan.append(count);
      const checked = relativeDate(r.menu_fetched_at);
      if (checked) {
        const freshness = document.createElement("div");
        freshness.style.cssText = "margin-top:2px;color:#a8a29e;font-size:11px";
        freshness.textContent = `Menu checked ${checked}`;
        vegan.append(freshness);
      }

      // — Actions: See dishes · Website —
      if (analyzed || r.website_url) {
        const actions = addSection();
        const row = document.createElement("div");
        row.style.cssText = "display:flex;gap:14px;align-items:center";
        if (r.website_url) {
          const site = document.createElement("a");
          site.textContent = "Website ↗";
          site.style.cssText =
            "color:#57534e;font-weight:700;font-size:13px;text-decoration:none";
          site.href = r.website_url;
          site.target = "_blank";
          site.rel = "noopener noreferrer";
          site.onmouseenter = () => (site.style.color = "#047857");
          site.onmouseleave = () => (site.style.color = "#57534e");
          row.append(site);
        }
        if (analyzed) {
          const btn = document.createElement("button");
          btn.textContent = "See dishes →";
          btn.style.cssText =
            "margin-left:auto;color:#047857;font-weight:700;cursor:pointer;background:none;border:none;padding:0;font-size:13px";
          btn.onclick = () => {
            setDishesFilter("all");
            setDishesTab(null);
            setDishesMention(null);
            setDishesCommentFilter(null);
            setDishesFor(r);
          };
          row.append(btn);
        }
        actions.append(row);
      }
      marker.bindPopup(el, { closeButton: false });
    });

    if (originLabel !== "Maitland") {
      // A chosen origin (address search or near-me) wins: center the map
      // there so it answers "what's around this spot", even when that spot
      // is far from every pin.
      map.setView([origin.lat, origin.lng], 13);
    } else if (bounds.length > 0) {
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
      className={`group relative flex cursor-pointer flex-col rounded-2xl border bg-white p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md ${
        scoreOpenFor === r.place_id ? "z-40" : ""
      } ${
        focus != null && focus.id != null && focus.id === r.place_id
          ? "border-emerald-600 ring-1 ring-emerald-600"
          : "border-stone-200/80"
      }`}
    >
      {/* One decision hierarchy: identity, vegan usefulness, practical
          metadata, then a single primary menu action. */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 text-base font-extrabold leading-snug text-stone-900">
          {r.name}
        </div>
        <FavoriteButton
          active={favorites.restaurants.includes(r.id)}
          onClick={() => toggleRestaurant(r.id)}
          label="restaurant"
        />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {r.vegan_score != null && r.dish_count > 0 && (
          <VeganScoreBadge
            r={r}
            open={scoreOpenFor === r.place_id}
            onToggle={setScoreOpenFor}
          />
        )}
        <span
          className={`text-xs font-bold ${
            r.dish_count > 0 ? "text-emerald-800" : "text-stone-400"
          }`}
        >
          {veganOptionSummary(r)}
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-stone-500">
        <span className="capitalize">
          {prettyType(r.primary_type) || "restaurant"}
        </span>
        {priceLevelSymbol(r.price_level) && (
          <span className="font-semibold text-stone-600" title="Google price level">
            {priceLevelSymbol(r.price_level)}
          </span>
        )}
        {ratingText(r.rating, r.user_rating_count) && (
          <span className="font-semibold text-stone-600" title="Google rating">
            <span className="text-amber-500">★</span>{" "}
            {Number(r.rating).toFixed(1)}
            <span className="font-normal text-stone-400">
              {" "}({formatRatingCount(r.user_rating_count) ?? "—"})
            </span>
          </span>
        )}
        {r.distance != null && (
          <span className="font-medium text-stone-500">
            · {r.distance.toFixed(1)} mi
          </span>
        )}
        {r.dish_count === 0 && r.serves_vegetarian === 1 && (
          <span className="font-medium text-emerald-700">✓ veg-friendly</span>
        )}
      </div>

      {(() => {
        // Google's business status outranks the hours math — don't tell
        // anyone a temporarily closed restaurant is "open now".
        if (r.business_status === "CLOSED_TEMPORARILY") {
          return (
            <div className="mt-1 text-xs font-bold text-rose-600">
              Temporarily closed
            </div>
          );
        }
        const openState = currentOpenState(
          r.open_now, r.enriched_at, r.opening_hours
        );
        const todayHours = todayOpeningHours(r.opening_hours);
        if (openState == null && !todayHours) return null;
        return (
          <div className="mt-2 text-xs font-medium text-stone-600">
            {openState != null && (
              <span
                className={`font-bold ${
                  openState ? "text-emerald-700" : "text-rose-600"
                }`}
              >
                {openState ? "Open now" : "Closed"}
              </span>
            )}
            {openState != null && todayHours && " · "}
            {todayHours && `Today: ${todayHours}`}
          </div>
        );
      })()}

      {r.address && (
        <div
          title={r.address}
          className="mt-1 flex min-w-0 items-center gap-1 text-xs text-stone-400"
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
          <span className="truncate">{r.address}</span>
        </div>
      )}

      {/* The routine "checked N days ago" chip is noise; only warn when the
          menu is actually stale. */}
      {isMenuStale(r.menu_fetched_at) && (
        <div className="mt-2">
          <FreshnessBadge fetchedAt={r.menu_fetched_at} compact />
        </div>
      )}
      {(r.website_url ||
        r.dish_count > 0 ||
        CLOUD_ENABLED) && (
        <div className="mt-auto grid grid-cols-3 items-center border-t border-stone-100 pt-3 text-xs font-semibold">
          {/* Community is always discoverable at bottom-left and opens the
              modal directly on its comments tab. Website remains centered
              between it and the dishes action. */}
          <div className="flex min-w-0 justify-start">
            {CLOUD_ENABLED && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setDishesFilter("all");
                  setDishesTab("comments");
                  setDishesMention(null);
                  setDishesCommentFilter(null);
                  setDishesFor(r);
                }}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-sky-200 bg-sky-50 px-2 py-1.5 text-xs font-semibold tabular-nums text-sky-700 transition hover:border-sky-300 hover:bg-sky-100"
                title={
                  (commentCounts?.get(r.place_id) || 0) > 0
                    ? `${commentCounts.get(r.place_id)} community note${
                        commentCounts.get(r.place_id) === 1 ? "" : "s"
                      } — comments, reviews, and chat about this place`
                    : "Add the first community note about this restaurant"
                }
                aria-label={`Open restaurant notes: ${
                  commentCounts?.get(r.place_id) || 0
                } note${
                  (commentCounts?.get(r.place_id) || 0) === 1 ? "" : "s"
                }`}
              >
                <NoteIcon className="h-4 w-4" />
                <span>{commentCounts?.get(r.place_id) || 0}</span>
              </button>
            )}
          </div>
          <div className="flex min-w-0 justify-center">
            {r.website_url && (
              <a
                href={r.website_url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="whitespace-nowrap text-stone-500 hover:text-emerald-700 hover:underline"
              >
                Website ↗
              </a>
            )}
          </div>
          <div className="flex min-w-0 justify-end">
            {r.dish_count > 0 && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setDishesFilter(
                    r.vegan_options > 0 || (r.vegan_sides || 0) > 0
                      ? "veganish"
                      : "all"
                  );
                  setDishesTab(null);
                  setDishesMention(null);
                  setDishesCommentFilter(null);
                  setDishesFor(r);
                }}
                className="shrink-0 rounded-lg border border-emerald-200 bg-emerald-50 px-2 py-1.5 text-xs font-semibold text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100"
              >
                Dishes →
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );

  const analyzed = restaurants.filter((r) => r.dish_count > 0).length;
  const totalVegan = restaurants.reduce((s, r) => s + (r.vegan_options || 0), 0);
  const totalVeganSides = restaurants.reduce(
    (sum, restaurant) => sum + (restaurant.vegan_sides || 0),
    0
  );
  const activeFilterCount =
    Number(cuisine !== "all") +
    Number(openFilter !== "all") +
    Number(priceTier > 0) +
    Number(maxMiles > 0);

  return (
    <div className={`mx-auto max-w-7xl px-4 ${embedded ? "pb-8 pt-5" : "py-8"}`}>
      {/* Hero */}
      {!embedded && <div className="mb-6">
        <h1 className="text-2xl font-extrabold tracking-tight text-stone-900 sm:text-4xl">
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

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <FilterSidebar
          open={filtersOpen}
          onToggle={() => setFiltersOpen((value) => !value)}
          activeCount={activeFilterCount}
        >
          <button
            onClick={clearFilters}
            disabled={activeFilterCount === 0 && !query && sortBy === "score"}
            className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-semibold text-stone-700 transition hover:border-rose-300 hover:text-rose-600 disabled:cursor-default disabled:opacity-40"
          >
            ↺ Reset all filters
          </button>
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
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
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
            className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
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
            className="w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
            aria-label="Filter by price level"
            title="Google price level; restaurants without one are hidden while a price filter is active"
          >
            {PRICE_TIERS.map((p) => (
              <option key={p.tier} value={p.tier}>
                {p.label}
              </option>
            ))}
          </select>
          <LocationPicker
            originLabel={originLabel}
            onOrigin={(point, label) => {
              setOrigin(point);
              setOriginLabel(label);
              // Picking a location means "what's near here" — surface the
              // closest restaurants instead of leaving the previous sort.
              setSortBy("distance");
            }}
          />
        </FilterSidebar>

        <div className="min-w-0 flex-1">
      <div className="mb-4 flex items-center gap-2">
      <div className="relative min-w-0 flex-1">
        <span className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-xl text-stone-400" aria-hidden="true">⌕</span>
        <input
          autoFocus
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search restaurants, cuisines, or addresses…"
          className="w-full rounded-2xl border border-stone-300 bg-white py-3 pl-12 pr-11 text-base shadow-sm outline-none placeholder:text-stone-400 focus:border-emerald-600 focus:ring-2 focus:ring-emerald-100"
          aria-label="Search restaurants"
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
      <NearMeIconButton
        className="lg:hidden"
        onOrigin={(point, label) => {
          setOrigin(point);
          setOriginLabel(label);
          setSortBy("distance");
        }}
      />
      </div>

      {/* Floating view flip (phones/tablets) — thumb-reachable and
          unmissable; desktop shows both panes so it doesn't render. */}
      <button
        onClick={() => setView(view === "list" ? "map" : "list")}
        className="fixed inset-x-0 bottom-5 z-30 mx-auto w-fit rounded-full bg-stone-900 px-5 py-2.5 text-sm font-bold text-white shadow-xl lg:hidden"
      >
        {view === "list" ? "🗺 Map" : "☰ List"}
      </button>

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
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
                  {onMap.map(renderCard)}
                </div>
              )}
              {offMap.length > 0 && (
                <>
                  <div className="mb-2 mt-8 text-xs font-medium uppercase tracking-wide text-stone-400">
                    Not on the map ({offMap.length})
                  </div>
                  <div className="grid grid-cols-1 gap-3 opacity-90 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
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
        </div>
      </div>

      {dishesFor && (
        <DishModal
          restaurant={dishesFor}
          onClose={() => {
            setDishesFor(null);
            setDishesTab(null);
            setDishesMention(null);
            setDishesCommentFilter(null);
          }}
          initialFilter={dishesFilter}
          initialTab={dishesTab}
          initialMention={dishesMention}
          initialCommentFilter={dishesCommentFilter}
          onOpenDish={(d) =>
            setDetailDish({
              ...d,
              restaurant_id: dishesFor.id,
              restaurant_name: dishesFor.name,
              place_id: dishesFor.place_id,
              address: dishesFor.address,
              website_url: dishesFor.website_url,
              lat: dishesFor.lat,
              lng: dishesFor.lng,
              rating: dishesFor.rating,
              user_rating_count: dishesFor.user_rating_count,
              open_now: dishesFor.open_now,
              enriched_at: dishesFor.enriched_at,
              opening_hours: dishesFor.opening_hours,
              menu_fetched_at: dishesFor.menu_fetched_at,
              primary_type: dishesFor.primary_type,
              price_level: dishesFor.price_level,
              distance: dishesFor.distance,
            })
          }
        />
      )}
      {detailDish && (
        <DishDetail
          dish={detailDish}
          onClose={() => setDetailDish(null)}
          shareUrl={`${window.location.origin}${window.location.pathname}#dishes?dish=${detailDish.id}`}
          favorite={favorites.dishes?.includes(detailDish.id)}
          onToggleFavorite={() => toggleDish(detailDish.id)}
          restaurantFavorite={favorites.restaurants.includes(
            detailDish.restaurant_id
          )}
          onToggleRestaurant={() => toggleRestaurant(detailDish.restaurant_id)}
          onAddComment={() => {
            setDetailDish(null);
            setDishesMention(detailDish);
            setDishesCommentFilter(null);
            setDishesTab("comments");
          }}
          onViewComments={() => {
            setDetailDish(null);
            setDishesMention(null);
            setDishesCommentFilter(detailDish);
            setDishesTab("comments");
          }}
          commentCount={
            dishMentionCounts.get(
              `${detailDish.place_id}:${dishKey(detailDish.name)}`
            ) || 0
          }
          onShowMap={() => {
            const placeId = detailDish.place_id;
            setDetailDish(null);
            setDishesFor(null);
            setDishesTab(null);
            setDishesMention(null);
            setDishesCommentFilter(null);
            if (!isDesktop) setView("map");
            if (placeId) setFocus({ id: placeId, ts: Date.now(), source: "card" });
          }}
        />
      )}
    </div>
  );
}
