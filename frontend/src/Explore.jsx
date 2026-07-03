import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import DishModal from "./DishModal.jsx";

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
  { key: "vegan", label: "Most vegan options" },
  { key: "distance", label: "Closest" },
  { key: "name", label: "Name A–Z" },
];

const RANGES = [
  { miles: 0, label: "Any distance" },
  { miles: 1, label: "Within 1 mi" },
  { miles: 2, label: "Within 2 mi" },
  { miles: 5, label: "Within 5 mi" },
  { miles: 10, label: "Within 10 mi" },
];

const LEGEND = [
  { color: "#047857", label: "3+ vegan options" },
  { color: "#65a30d", label: "1–2 vegan options" },
  { color: "#78716c", label: "No vegan options found" },
  { color: "#a8a29e", label: "Menu not analyzed" },
];

export default function Explore() {
  const [restaurants, setRestaurants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [sortBy, setSortBy] = useState("vegan");
  const [maxMiles, setMaxMiles] = useState(0);
  const [origin, setOrigin] = useState(MAITLAND);
  const [originLabel, setOriginLabel] = useState("Maitland");
  const [locating, setLocating] = useState(false);
  const [view, setView] = useState("list"); // mobile toggle; desktop shows both
  const [isDesktop, setIsDesktop] = useState(
    () => window.matchMedia("(min-width: 1024px)").matches
  );
  const [dishesFor, setDishesFor] = useState(null);
  const mapEl = useRef(null);

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
    if (maxMiles > 0) {
      out = out.filter((r) => r.distance != null && r.distance <= maxMiles);
    }
    return [...out].sort((a, b) => {
      if (sortBy === "vegan") {
        if ((b.vegan_options || 0) !== (a.vegan_options || 0))
          return (b.vegan_options || 0) - (a.vegan_options || 0);
        return (a.distance ?? 1e9) - (b.distance ?? 1e9);
      }
      if (sortBy === "distance") return (a.distance ?? 1e9) - (b.distance ?? 1e9);
      return (a.name || "").localeCompare(b.name || "");
    });
  }, [enriched, query, maxMiles, sortBy]);

  const showMap = isDesktop || view === "map";

  // (Re)build the map whenever it's visible and inputs change. Cheap at this
  // scale (~60 markers), and much simpler than incremental marker sync.
  useEffect(() => {
    if (!showMap || !mapEl.current) return;
    const map = L.map(mapEl.current, { zoomControl: true });
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
      bounds.push([r.lat, r.lng]);

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
      sub.textContent = prettyType(r.primary_type) || "restaurant";
      const count = document.createElement("div");
      count.style.cssText = `margin-top:4px;font-size:13px;font-weight:600;color:${
        (r.vegan_options || 0) > 0 ? "#047857" : "#57534e"
      }`;
      count.textContent = analyzed
        ? `${r.vegan_options || 0} vegan food option${r.vegan_options === 1 ? "" : "s"}`
        : "Menu not analyzed yet";
      el.append(title, sub, count);
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
    setTimeout(() => map.invalidateSize(), 100);
    return () => map.remove();
  }, [showMap, filtered, origin, originLabel]);

  const analyzed = restaurants.filter((r) => r.dish_count > 0).length;
  const totalVegan = restaurants.reduce((s, r) => s + (r.vegan_options || 0), 0);

  return (
    <div className="mx-auto max-w-7xl px-4 py-8">
      {/* Hero */}
      <div className="mb-6">
        <h1 className="text-3xl font-extrabold tracking-tight text-stone-900 sm:text-4xl">
          Find <span className="text-emerald-700">vegan-friendly</span> dishes
          near you
        </h1>
        <p className="mt-2 text-sm text-stone-500">
          <span className="font-semibold text-stone-700">{totalVegan}</span>{" "}
          vegan food options found across{" "}
          <span className="font-semibold text-stone-700">{analyzed}</span>{" "}
          analyzed menus — every verdict backed by menu evidence, even when the
          restaurant never says "vegan".
        </p>
      </div>

      {/* Filter bar */}
      <div className="sticky top-[60px] z-10 -mx-4 mb-6 border-y border-stone-200/70 bg-[#faf8f4]/95 px-4 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search name, cuisine, address…"
            className="w-full max-w-xs rounded-full border border-stone-300 bg-white px-4 py-2 text-sm shadow-sm outline-none placeholder:text-stone-400 focus:border-emerald-600 focus:ring-1 focus:ring-emerald-600"
          />
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
                {filtered.length} restaurant{filtered.length === 1 ? "" : "s"}
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
                {filtered.map((r) => (
                  <div
                    key={r.place_id}
                    className="group flex flex-col rounded-2xl border border-stone-200/80 bg-white p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="font-bold leading-snug text-stone-900">
                        {r.name}
                      </div>
                      {r.distance != null && (
                        <span className="shrink-0 rounded-full bg-stone-100 px-2 py-0.5 text-xs font-medium text-stone-500">
                          {r.distance.toFixed(1)} mi
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 text-xs capitalize text-stone-500">
                      {prettyType(r.primary_type) || "restaurant"}
                      {r.serves_vegetarian === 1 && (
                        <span className="ml-2 font-medium text-emerald-700">
                          ✓ veg-friendly
                        </span>
                      )}
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
                          {r.vegan_options} vegan option
                          {r.vegan_options === 1 ? "" : "s"}
                        </span>
                      ) : (
                        <span className="text-xs italic text-stone-400">
                          menu not analyzed yet
                        </span>
                      )}
                      {r.dish_count > 0 && (
                        <button
                          onClick={() => setDishesFor(r)}
                          className="text-xs font-bold text-emerald-700 opacity-80 transition group-hover:opacity-100 hover:underline"
                        >
                          See dishes →
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
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
