import { VENUE_KIND_OPTIONS } from "./cuisine.js";
import { venueIconMarkup } from "./venueMarkers.js";

export default function VenueTypeLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
      {VENUE_KIND_OPTIONS.map((option) => (
        <span
          key={option.key}
          className="inline-flex items-center gap-1 text-[11px] font-semibold text-stone-600"
        >
          <span
            className={`vf-venue-key vf-place-pin__type--${option.key}`}
            dangerouslySetInnerHTML={{ __html: venueIconMarkup(option.key) }}
          />
          {option.shortLabel}
        </span>
      ))}
    </div>
  );
}
