const ICON_PATHS = Object.freeze({
  restaurant:
    '<path d="M3 1v4M5 1v4M4 1v13M2 1v3a2 2 0 0 0 4 0V1M11 1v13M11 1c2 1.5 2.5 5 0 7"/>',
  coffee:
    '<path d="M2 5h9v4a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V5Zm9 1h1a2 2 0 0 1 0 4h-1M4 1c1 1-.5 1.5.5 2.5M7 1c1 1-.5 1.5.5 2.5"/>',
  dessert:
    '<path d="M5 7h6l-3 7-3-7Zm-1 0a4 4 0 0 1 8 0M6 4.5A2.8 2.8 0 0 1 8 2a2.8 2.8 0 0 1 2 2.5"/>',
});

function safeKind(kind) {
  return Object.hasOwn(ICON_PATHS, kind) ? kind : "restaurant";
}

export function venueIconMarkup(kind) {
  const normalized = safeKind(kind);
  return `<svg class="vf-venue-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[normalized]}</svg>`;
}

export function venueMarkerHtml({
  kind,
  count,
  color,
  analyzed = true,
}) {
  const normalized = safeKind(kind);
  const safeColor = /^#[0-9a-f]{6}$/i.test(String(color || ""))
    ? color
    : "#78716c";
  const numericCount = Number(count);
  const label =
    analyzed && Number.isFinite(numericCount)
      ? String(Math.max(0, Math.trunc(numericCount)))
      : "?";
  return `<div class="vf-place-pin" style="--vf-pin-color:${safeColor}"><span class="vf-place-pin__type vf-place-pin__type--${normalized}">${venueIconMarkup(normalized)}</span><span class="vf-place-pin__count">${label}</span></div>`;
}

export const VENUE_MARKER_SIZE = [44, 28];
export const VENUE_MARKER_ANCHOR = [22, 14];
