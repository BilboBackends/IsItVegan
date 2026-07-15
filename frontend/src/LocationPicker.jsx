import { useId, useState } from "react";

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

function geolocationErrorMessage(error) {
  if (error?.code === 1) {
    return "Location access is blocked. Allow location for DishTune in your browser settings, then try again.";
  }
  if (error?.code === 3) {
    return "Getting your location timed out. Try again somewhere with a clearer signal.";
  }
  return "Couldn’t get your location. Check location services and try again.";
}

// Compact one-tap "use my location" button for MOBILE, where the full
// LocationPicker sits behind the collapsed filter sidebar — near-me must be
// immediately reachable, not two taps deep.
export function NearMeIconButton({ onOrigin, className = "" }) {
  const [locating, setLocating] = useState(false);
  const [error, setError] = useState(null);
  const errorId = useId();

  function locate() {
    if (locating) return;
    if (!navigator.geolocation) {
      setError("Location is not available in this browser.");
      return;
    }
    setError(null);
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        onOrigin(
          { lat: pos.coords.latitude, lng: pos.coords.longitude },
          "your location"
        );
        setLocating(false);
      },
      (reason) => {
        setLocating(false);
        setError(geolocationErrorMessage(reason));
      },
      { timeout: 10000, maximumAge: 300000 }
    );
  }

  return (
    <div className={`relative shrink-0 ${className}`}>
      <button
        type="button"
        onClick={locate}
        disabled={locating}
        title="Use my location"
        aria-label="Use my location"
        aria-busy={locating}
        aria-describedby={error ? errorId : undefined}
        className="flex h-[50px] w-[50px] items-center justify-center rounded-2xl border border-stone-300 bg-white text-xl shadow-sm transition active:scale-95 disabled:opacity-50"
      >
        {locating ? (
          <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-emerald-600 border-t-transparent" />
        ) : (
          "📍"
        )}
      </button>
      {error && (
        <div
          id={errorId}
          role="status"
          aria-live="polite"
          className="absolute right-0 top-full z-[1000] mt-2 w-64 rounded-xl border border-rose-200 bg-white py-2 pl-3 pr-9 text-xs font-medium leading-relaxed text-rose-700 shadow-lg"
        >
          {error}
          <button
            type="button"
            onClick={() => setError(null)}
            aria-label="Dismiss location error"
            className="absolute right-1.5 top-1.5 flex h-7 w-7 items-center justify-center rounded-full text-lg leading-none text-rose-500 hover:bg-rose-50 hover:text-rose-700"
          >
            &times;
          </button>
        </div>
      )}
    </div>
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
      (reason) => {
        setLocating(false);
        setError(geolocationErrorMessage(reason));
      },
      { timeout: 10000, maximumAge: 300000 }
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
        <div
          role="status"
          aria-live="polite"
          className="mt-1 flex items-start justify-center gap-1 text-center text-xs text-rose-500"
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            aria-label="Dismiss location error"
            className="-mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-base leading-none hover:bg-rose-50 hover:text-rose-700"
          >
            &times;
          </button>
        </div>
      )}
      {!compact && (
        <div className="mt-1 text-center text-xs text-stone-400">
          Distances from {originLabel}
        </div>
      )}
    </div>
  );
}
