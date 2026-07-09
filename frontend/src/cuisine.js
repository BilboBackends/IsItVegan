const GROUPS = [
  ["Japanese", ["japanese", "sushi"]],
  ["Seafood", ["seafood"]],
  ["Italian & Pizza", ["italian", "pizza"]],
  ["Thai", ["thai"]],
  ["Indian", ["indian"]],
  ["Chinese", ["chinese"]],
  ["Korean", ["korean"]],
  ["Vietnamese", ["vietnamese"]],
  ["Mediterranean", ["mediterranean", "greek", "middle_eastern"]],
  ["Mexican & Tacos", ["mexican", "taco"]],
  ["Barbecue", ["barbecue"]],
  ["American", ["american", "hot_dog"]],
  ["Sandwiches & Deli", ["sandwich", "deli"]],
  ["Cafe & Breakfast", ["cafe", "coffee", "breakfast"]],
  ["Pub & Brewery", ["pub", "bar", "brewery"]],
  ["Fast Food", ["fast_food"]],
  ["Vegan", ["vegan"]],
];

export function cuisineLabel(primaryType) {
  const type = (primaryType || "").toLowerCase();
  if (!type || type === "restaurant") return "Other";
  for (const [label, matches] of GROUPS) {
    if (matches.some((value) => type.includes(value))) return label;
  }
  const fallback = type
    .replaceAll("_", " ")
    .replace(/\b(restaurant|shop)\b/g, "")
    .trim();
  return fallback
    ? fallback.replace(/\b\w/g, (letter) => letter.toUpperCase())
    : "Other";
}

// Venues whose product IS dessert. Their headline vegan count includes
// desserts (backend: db.DESSERT_VENUE_TYPES — keep the two lists in sync),
// and the UI labels those counts "vegan treats" instead of "vegan meals".
const DESSERT_VENUE_TYPES = new Set([
  "ice_cream_shop", "dessert_shop", "dessert_restaurant", "bakery",
  "donut_shop", "bagel_shop", "chocolate_shop", "chocolate_factory",
  "candy_store", "confectionery", "frozen_yogurt_shop", "acai_shop",
]);

export function isDessertVenue(primaryType) {
  return DESSERT_VENUE_TYPES.has((primaryType || "").toLowerCase());
}

export function cuisineOptions(items) {
  return [...new Set(items.map((item) => cuisineLabel(item.primary_type)))].sort(
    (a, b) => a.localeCompare(b)
  );
}
