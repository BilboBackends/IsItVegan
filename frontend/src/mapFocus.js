export const PLACE_FOCUS_ZOOM = 16;
export const MAP_FOCUS_MAX_AGE_MS = 2_500;

export function isFreshMapFocus(focus, now = Date.now()) {
  const timestamp = focus?.timestamp ?? focus?.ts;
  return Number.isFinite(timestamp) && now - timestamp <= MAP_FOCUS_MAX_AGE_MS;
}

// Bring a selected venue into a useful street-level view without pulling the
// user back out when they have already zoomed in for more detail.
export function placeFocusZoom(currentZoom) {
  const zoom = Number(currentZoom);
  return Number.isFinite(zoom)
    ? Math.max(zoom, PLACE_FOCUS_ZOOM)
    : PLACE_FOCUS_ZOOM;
}

export function focusMapOnMarker(map, marker) {
  if (!map || !marker) return;
  map.flyTo(marker.getLatLng(), placeFocusZoom(map.getZoom()), {
    duration: 0.8,
  });
}
