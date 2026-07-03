import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import DishModal from "./DishModal.jsx";

// Consumer-facing view: find vegan-friendly dishes near you. Search, sort,
// distance filter, and an OpenStreetMap map (Leaflet — no API key, so nothing
// secret ever reaches the browser, per CLAUDE.md).

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

const SORTS = [
  { key: "vegan", label: "Most vegan options" },
  { key: "distance", label: "Closest" },
  { key: "name", label: "Name" },
];

const RANGES = [
  { miles: 0, label: "Any distance" },
  { miles: 1, label: "Within 1 mi" },
  { miles: 2, label: "Within 2 mi" },
  { miles: 5, label: "Within 5 mi" },
  { miles: 10, label: "Within 10 mi" },
];

export default function Explore() {
  const [restaurants, setRestaurants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [sortBy, setSortBy] = useState("vegan");
  const [maxMiles, setMaxMiles] = useState(0);
  const [origin, setOrigin] = useState(MAITLAND);
  const [originLabel, setOriginLabel] = useState("Maitland center");
  const [locating, setLocating] = useState(false);
  const [view, setView] = useState("list"); // list | map
  const [dishesFor, setDishesFor] = useState(null);
  const mapEl = useRef(null);

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
      if (sortBy === "distance")
        return (a.distance ?? 1e9) - (b.distance ?? 1e9);
      return (a.name || "").localeCompare(b.name || "");
    });
  }, [enriched, query, maxMiles, sortBy]);

  // Build the map when the map view is active. Rebuilding on filter changes
  // is cheap at this scale (~60 markers) and keeps the logic simple.
  useEffect(() => {
    if (view !== "map" || !mapEl.current) return;
    const map = L.map(mapEl.current).setView([origin.lat, origin.lng], 13);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors",
    }).addTo(map);

    L.circleMarker([origin.lat, origin.lng], {
      radius: 6,
      color: "#2563eb",
      fillColor: "#3b82f6",
      fillOpacity: 0.9,
    })
      .addTo(map)
      .bindTooltip(originLabel);

    filtered.forEach((r) => {
      if (r.lat == null || r.lng == null) return;
      const color =
        (r.vegan_options || 0) > 2
          ? "#059669"
          : (r.vegan_options || 0) > 0
            ? "#65a30d"
            : "#94a3b8";
      const marker = L.circleMarker([r.lat, r.lng], {
        radius: 9,
        color,
        fillColor: color,
        fillOpacity: 0.75,
        weight: 2,
      }).addTo(map);

      // Build popup DOM with textContent (never innerHTML with API data).
      const el = document.createElement("div");
      const title = document.createElement("div");
      title.style.fontWeight = "600";
      title.textContent = r.name || "";
      const count = document.createElement("div");
      count.style.fontSize = "13px";
      count.textContent = `${r.vegan_options || 0} vegan option${
        r.vegan_options === 1 ? "" : "s"
      }${r.dish_count ? ` of ${r.dish_count} items` : " (menu not analyzed yet)"}`;
      const addr = document.createElement("div");
      addr.style.cssText = "color:#64748b;font-size:12px";
      addr.textContent = r.address || "";
      el.append(title, count, addr);
      if (r.dish_count > 0) {
        const btn = document.createElement("button");
        btn.textContent = "See dishes →";
        btn.style.cssText =
          "margin-top:4px;color:#059669;font-weight:600;cursor:pointer;background:none;border:none;padding:0;font-size:13px";
        btn.onclick = () => setDishesFor(r);
        el.append(btn);
      }
      marker.bindPopup(el);
    });

    setTimeout(() => map.invalidateSize(), 100);
    return () => map.remove();
  }, [view, filtered, origin, originLabel]);

  const analyzed = restaurants.filter((r) => r.dish_count > 0).length;

  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <div className="mb-1 text-2xl font-bold text-slate-900">
        Find vegan-friendly dishes
      </div>
      <p className="mb-5 text-sm text-slate-500">
        {restaurants.length} restaurants around Maitland · {analyzed} menus
        analyzed · verdicts backed by menu evidence
      </p>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search name, cuisine, address…"
          className="w-full max-w-xs rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
        />
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value)}
          className="rounded-lg border border-slate-300 px-2 py-2 text-sm"
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
          className="rounded-lg border border-slate-300 px-2 py-2 text-sm"
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
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:text-slate-400"
          title={`Distances measured from ${originLabel}`}
        >
          {locating ? "Locating…" : "📍 Use my location"}
        </button>
        <div className="ml-auto flex overflow-hidden rounded-lg border border-slate-300">
          {["list", "map"].map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`px-3 py-2 text-sm font-medium capitalize ${
                view === v
                  ? "bg-emerald-600 text-white"
                  : "bg-white text-slate-600 hover:bg-slate-50"
              }`}
            >
              {v}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {view === "map" ? (
        <div
          ref={mapEl}
          className="h-[70vh] w-full overflow-hidden rounded-xl border border-slate-200 shadow-sm"
        />
      ) : loading ? (
        <div className="p-10 text-center text-slate-400">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="p-10 text-center text-slate-400">
          No restaurants match.
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((r) => (
            <div
              key={r.place_id}
              className="flex flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="font-semibold text-slate-900">{r.name}</div>
                {r.distance != null && (
                  <span className="shrink-0 text-xs text-slate-400">
                    {r.distance.toFixed(1)} mi
                  </span>
                )}
              </div>
              <div className="mt-0.5 text-xs capitalize text-slate-500">
                {prettyType(r.primary_type) || "restaurant"}
                {r.serves_vegetarian === 1 && (
                  <span className="ml-2 text-emerald-600">
                    ✓ vegetarian-friendly
                  </span>
                )}
              </div>
              <div className="mt-1 text-xs text-slate-400">{r.address}</div>
              {r.editorial_summary && (
                <div className="mt-2 line-clamp-2 text-xs text-slate-500">
                  {r.editorial_summary}
                </div>
              )}
              <div className="mt-3 flex items-center justify-between pt-1">
                {r.dish_count > 0 ? (
                  <span
                    className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                      r.vegan_options > 0
                        ? "bg-emerald-100 text-emerald-800"
                        : "bg-slate-100 text-slate-500"
                    }`}
                  >
                    {r.vegan_options} vegan option
                    {r.vegan_options === 1 ? "" : "s"}
                  </span>
                ) : (
                  <span className="text-xs text-slate-400">
                    menu not analyzed yet
                  </span>
                )}
                {r.dish_count > 0 && (
                  <button
                    onClick={() => setDishesFor(r)}
                    className="text-xs font-semibold text-emerald-700 hover:underline"
                  >
                    See dishes →
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {dishesFor && (
        <DishModal restaurant={dishesFor} onClose={() => setDishesFor(null)} />
      )}
    </div>
  );
}
