import { VENUE_KIND_OPTIONS } from "./cuisine.js";
import { venueIconMarkup } from "./venueMarkers.js";

const ALL_OPTION = { key: "all", shortLabel: "All" };

function AllPlacesIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      className="h-[18px] w-[18px]"
      aria-hidden="true"
    >
      <rect x="3" y="3" width="5" height="5" rx="1" />
      <rect x="12" y="3" width="5" height="5" rx="1" />
      <rect x="3" y="12" width="5" height="5" rx="1" />
      <rect x="12" y="12" width="5" height="5" rx="1" />
    </svg>
  );
}

// Keep this high-level choice visible above both the list and map. Detailed
// controls can live in the sidebar, but users should never have to hunt for
// the basic restaurant / coffee / dessert distinction shown by the pins.
export default function VenueKindFilter({ value = "all", onChange }) {
  return (
    <div
      className="mb-4 grid grid-cols-4 gap-1 rounded-xl border border-stone-200 bg-white p-1 shadow-sm"
      aria-label="Filter by place type"
    >
      {[ALL_OPTION, ...VENUE_KIND_OPTIONS].map((option) => {
        const active = value === option.key;
        return (
          <button
            key={option.key}
            type="button"
            onClick={() => onChange(option.key)}
            aria-pressed={active}
            className={`inline-flex min-w-0 flex-col items-center justify-center gap-0.5 rounded-lg px-1 py-1.5 text-[11px] font-bold transition sm:flex-row sm:gap-1.5 sm:px-3 sm:py-2 sm:text-sm ${
              active
                ? "bg-emerald-700 text-white shadow-sm"
                : "text-stone-600 hover:bg-stone-100 hover:text-stone-900"
            }`}
          >
            {option.key === "all" ? (
              <AllPlacesIcon />
            ) : (
              <span
                className={`vf-venue-key vf-place-pin__type--${option.key} ${
                  active ? "bg-white/20 text-white" : ""
                }`}
                dangerouslySetInnerHTML={{ __html: venueIconMarkup(option.key) }}
                aria-hidden="true"
              />
            )}
            {option.shortLabel}
          </button>
        );
      })}
    </div>
  );
}
