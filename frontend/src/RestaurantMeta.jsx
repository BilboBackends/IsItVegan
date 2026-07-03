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

export function OpenStatusBadge({ openNow, enrichedAt }) {
  if (openNow == null) return null;
  const hoursAreFresh = (ageInDays(enrichedAt) ?? Infinity) < 1;
  if (!hoursAreFresh) {
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
        openNow === 1 || openNow === true
          ? "bg-emerald-100 text-emerald-800"
          : "bg-rose-50 text-rose-700"
      }`}
    >
      {openNow === 1 || openNow === true ? "Open now" : "Closed"}
    </span>
  );
}
