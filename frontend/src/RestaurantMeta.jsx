import {
  openingHoursSnapshot,
  scheduledOpenState,
  todayHoursText,
} from "./openingHours.js";

function ageInDays(value) {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, (Date.now() - timestamp) / 86_400_000);
}

export function isMenuStale(value, days = 30) {
  const age = ageInDays(value);
  return age != null && age > days;
}

export function relativeDate(value) {
  const age = ageInDays(value);
  if (age == null) return null;
  if (age < 1) return "today";
  if (age < 2) return "yesterday";
  if (age < 30) return `${Math.floor(age)} days ago`;
  if (age < 60) return "1 month ago";
  return `${Math.floor(age / 30)} months ago`;
}

export function todayOpeningHours(openingHours, date = new Date()) {
  return todayHoursText(openingHours, date);
}

export function TodayHours({ openingHours, className = "" }) {
  const hours = todayOpeningHours(openingHours);
  if (!hours) return null;
  return (
    <span
      className={className}
      title={openingHours.join("\n")}
    >
      Today: {hours}
    </span>
  );
}

export function currentOpenState(openNow, enrichedAt, openingHours, date = new Date()) {
  const scheduled = scheduledOpenState(openingHours, date);
  if (scheduled != null) return scheduled;
  if (openNow == null || (ageInDays(enrichedAt) ?? Infinity) >= 1) return null;
  return openNow === 1 || openNow === true;
}

export function restaurantOpenSnapshot(
  openNow,
  enrichedAt,
  openingHours,
  date = new Date()
) {
  const hours = openingHoursSnapshot(openingHours, date);
  let openState = hours.scheduledOpenState;
  if (openState == null) {
    openState =
      openNow == null || (ageInDays(enrichedAt) ?? Infinity) >= 1
        ? null
        : openNow === 1 || openNow === true;
  }
  return {
    openState,
    scheduleState: hours.scheduledOpenState,
    todayHours: hours.todayHours,
  };
}

export function FreshnessBadge({ fetchedAt, compact = false }) {
  const label = relativeDate(fetchedAt);
  if (!label) return null;
  const stale = isMenuStale(fetchedAt);
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        stale ? "bg-amber-100 text-amber-800" : "bg-stone-100 text-stone-500"
      }`}
      title={`Menu text last checked ${new Date(fetchedAt).toLocaleString()}`}
    >
      {stale ? "Menu may be stale" : compact ? `Checked ${label}` : `Menu checked ${label}`}
    </span>
  );
}

export function OpenStatusBadge({ openNow, enrichedAt, openingHours }) {
  const { scheduleState, openState } = restaurantOpenSnapshot(
    openNow,
    enrichedAt,
    openingHours
  );
  if (openState == null) {
    if (openNow == null && !openingHours?.length) return null;
    return (
      <span
        className="inline-flex items-center rounded-full bg-stone-100 px-2 py-0.5 text-xs font-medium text-stone-500"
        title="Opening status needs a Google refresh"
      >
        Hours available
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-bold ${
        openState
          ? "bg-emerald-100 text-emerald-800"
          : "bg-rose-50 text-rose-700"
      }`}
      title={
        scheduleState != null
          ? "Calculated locally from the restaurant's listed weekly hours"
          : "Current status from the latest Google refresh"
      }
    >
      {openState ? "Open now" : "Closed"}
    </span>
  );
}
