const INDIVIDUAL_MARKER_ZOOM = 13;

// A lightweight grid cluster keeps hundreds of Leaflet marker DOM nodes off
// phones while zoomed out. At neighborhood zoom every restaurant gets its
// normal typed marker again.
export function aggregateMapItems(
  items,
  zoom,
  getId = (item) => item.id,
  priorityId = null
) {
  if (!Array.isArray(items) || items.length === 0) return [];
  if (!Number.isFinite(zoom) || zoom >= INDIVIDUAL_MARKER_ZOOM) {
    return items.map((item) => ({
      key: `item:${getId(item)}`,
      lat: item.lat,
      lng: item.lng,
      items: [item],
      cluster: false,
    }));
  }

  // A restaurant selected from a card must have its own marker immediately,
  // even while the rest of its neighborhood remains clustered. This keeps
  // the card -> pin -> popup interaction reliable without giving up the DOM
  // savings from clustering every other restaurant.
  const priorityKey = priorityId == null ? null : String(priorityId);
  let priorityItem = null;
  const clusterableItems = [];
  for (const item of items) {
    if (priorityItem == null && String(getId(item)) === priorityKey) {
      priorityItem = item;
    } else {
      clusterableItems.push(item);
    }
  }

  // Roughly a 70px screen grid expressed in longitude degrees. This is not
  // geographic analysis; it is only a rendering reduction for nearby pins.
  const cellSize = (360 / 2 ** Math.max(1, zoom)) * 0.28;
  const groups = new Map();
  for (const item of clusterableItems) {
    if (item.lat == null || item.lng == null) continue;
    const row = Math.floor(item.lat / cellSize);
    const column = Math.floor(item.lng / cellSize);
    const key = `${row}:${column}`;
    const group = groups.get(key) || { items: [], latTotal: 0, lngTotal: 0 };
    group.items.push(item);
    group.latTotal += item.lat;
    group.lngTotal += item.lng;
    groups.set(key, group);
  }

  const entries = [...groups.entries()].map(([cell, group]) => {
    const members = [...group.items].sort((a, b) =>
      String(getId(a)).localeCompare(String(getId(b)))
    );
    if (members.length === 1) {
      const item = members[0];
      return {
        key: `item:${getId(item)}`,
        lat: item.lat,
        lng: item.lng,
        items: members,
        cluster: false,
      };
    }
    return {
      key: `cluster:${cell}:${members.map(getId).join(",")}`,
      lat: group.latTotal / members.length,
      lng: group.lngTotal / members.length,
      items: members,
      cluster: true,
    };
  });
  if (priorityItem) {
    entries.push({
      key: `item:${getId(priorityItem)}`,
      lat: priorityItem.lat,
      lng: priorityItem.lng,
      items: [priorityItem],
      cluster: false,
    });
  }
  return entries;
}

// Viewport culling is useful at street-level zoom, but a card may select a
// restaurant outside the current viewport. Keep just that one item mounted so
// Leaflet can fly to its exact marker and open the bound popup.
export function withPriorityMapItem(
  items,
  priorityItem,
  getId = (item) => item.id
) {
  if (!priorityItem) return items || [];
  const currentItems = items || [];
  const priorityId = String(getId(priorityItem));
  if (currentItems.some((item) => String(getId(item)) === priorityId)) {
    return currentItems;
  }
  return [...currentItems, priorityItem];
}

export function clusterMarkerHtml(count) {
  return `<div style="display:grid;place-items:center;width:36px;height:36px;border:3px solid white;border-radius:9999px;background:#065f46;color:white;font:700 12px/1 system-ui;box-shadow:0 2px 8px rgba(0,0,0,.28)">${Number(count).toLocaleString()}</div>`;
}

export function mapItemsForViewport(items, zoom, bounds, paddingRatio = 0.25) {
  if (
    !Array.isArray(items) ||
    zoom < INDIVIDUAL_MARKER_ZOOM ||
    !bounds
  ) {
    return items || [];
  }
  const latitudePadding = Math.max(0, bounds.n - bounds.s) * paddingRatio;
  const longitudePadding = Math.max(0, bounds.e - bounds.w) * paddingRatio;
  return items.filter(
    (item) =>
      item.lat >= bounds.s - latitudePadding &&
      item.lat <= bounds.n + latitudePadding &&
      item.lng >= bounds.w - longitudePadding &&
      item.lng <= bounds.e + longitudePadding
  );
}

export const MAP_INDIVIDUAL_MARKER_ZOOM = INDIVIDUAL_MARKER_ZOOM;
