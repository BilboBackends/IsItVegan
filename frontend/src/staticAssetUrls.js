export function staticAssetUrl(path, base = "/") {
  if (path === "/api/restaurants") return `${base}data/restaurants.json`;
  if (path === "/api/data-manifest") return `${base}data/manifest.json`;
  if (path === "/api/dishes") return `${base}data/dishes.json`;
  // The historical dishes.json.gz remains a full-schema compatibility asset
  // for browser tabs running the old bundle. New code requests compact v2.
  if (path === "/api/dishes.gz") return `${base}data/dishes-v2.json.gz`;
  return path;
}

export function appendDataVersion(url, version) {
  if (!version) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}v=${encodeURIComponent(version)}`;
}
