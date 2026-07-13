import { useState } from "react";

// Shared origin picker for the Explore views: type an address OR use browser
// geolocation. Geocoding calls Nominatim (OpenStreetMap) straight from the
// browser — the public static site has no backend, and Nominatim needs no
// API key, so nothing secret ships client-side. Volume is a couple of
// lookups per visit, well inside Nominatim's fair-use policy.
const NOMINATIM = "https://nominatim.openstreetmap.org/search";

// Bias (never restrict) matches toward Central Florida so a bare street or
// neighborhood name resolves near the restaurants, not a same-named road in
// another state. left,top,right,bottom around greater Orlando.
const ORLANDO_VIEWBOX = "-81.65,28.85,-81.15,28.30";

// Compact one-tap "use my location" button for MOBILE, where the full
// LocationPicker sits behind the collapsed filter sidebar — near-me must be
// immediately reachable, not two taps deep.
export function NearMeIconButton({ onOrigin, className = "" }) {
  const [locating, setLocating] = useState(false);

  function locate() {
    if (!navigator.geolocation || locating) return;
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        onOrigin(
          { lat: pos.coords.latitude, lng: pos.coords.longitude },
          "your location"
        );
        setLocating(false);
      },
      () => setLocating(false),
      { timeout: 8000 }
    );
  }

  return (
    <button
      type="button"
      onClick={locate}
      disabled={locating}
      title="Use my location"
      aria-label="Use my location"
      className={`flex h-[50px] w-[50px] shrink-0 items-center justify-center rounded-2xl border border-stone-300 bg-white text-xl shadow-sm transition active:scale-95 disabled:opacity-50 ${className}`}
    >
      {locating ? (
        <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-emerald-600 border-t-transparent" />
      ) : (
        "📍"
      )}
    </button>
  );
}

// compact: address form only — for the phone's inline "change origin" row,
// where geolocation already has its own one-tap pin button and the current
// origin label lives on the toggle line itself.
export default function LocationPicker({ originLabel, onOrigin, compact = false }) {
  const [address, setAddress] = useState("");
  const [locating, setLocating] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState(null);

  function useMyLocation() {
    if (!navigator.geolocation) return;
    setLocating(true);
    setError(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        onOrigin({ lat: pos.coords.latitude, lng: pos.coords.longitude }, "your location");
        setLocating(false);
      },
      () => setLocating(false),
      { timeout: 8000 }
    );
  }

  async function searchAddress(event) {
    event.preventDefault();
    const q = address.trim();
    if (!q || searching) return;
    setSearching(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        format: "jsonv2",
        limit: "1",
        q,
        viewbox: ORLANDO_VIEWBOX,
        bounded: "0",
      });
      const res = await fetch(`${NOMINATIM}?${params}`, {
        headers: { Accept: "application/json" },
      });
      const results = res.ok ? await res.json() : [];
      if (!Array.isArray(results) || results.length === 0) {
        setError("No match — try adding a city or zip.");
        return;
      }
      const hit = results[0];
      // display_name is a full "road, suburb, county, state…" chain; the
      // first two segments are enough to recognize the place.
      const label =
        (hit.display_name || q).split(",").slice(0, 2).join(",").trim() || q;
      onOrigin({ lat: Number(hit.lat), lng: Number(hit.lon) }, label);
    } catch {
      setError("Address lookup failed — try again.");
    } finally {
      setSearching(false);
    }
  }

  return (
    <div>
      <form onSubmit={searchAddress} className="flex gap-1.5">
        <input
          type="text"
          value={address}
          autoFocus={compact}
          onChange={(event) => {
            setAddress(event.target.value);
            if (error) setError(null);
          }}
          placeholder="Address or area…"
          aria-label="Measure distances from this address"
          className="w-full min-w-0 rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm outline-none placeholder:text-stone-400 focus:border-emerald-600"
        />
        <button
          type="submit"
          disabled={searching || !address.trim()}
          className="shrink-0 rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-medium text-stone-700 hover:border-emerald-600 hover:text-emerald-700 disabled:text-stone-400"
        >
          {searching ? "…" : "Go"}
        </button>
      </form>
      {!compact && (
        <button
          type="button"
          onClick={useMyLocation}
          disabled={locating}
          className="mt-1.5 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-medium text-stone-700 hover:border-emerald-600 hover:text-emerald-700 disabled:text-stone-400"
          title={`Distances measured from ${originLabel}`}
        >
          {locating ? "Locating…" : "📍 Near me"}
        </button>
      )}
      {error && (
        <div className="mt-1 text-center text-xs text-rose-500">{error}</div>
      )}
      {!compact && (
        <div className="mt-1 text-center text-xs text-stone-400">
          Distances from {originLabel}
        </div>
      )}
    </div>
  );
}
