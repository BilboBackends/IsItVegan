// Every Google primary_type maps into one of these broad groups; anything
// unmatched becomes "Other" instead of minting a one-off dropdown option
// ("Coworking Space", "Burrito", "Hotel"…), so the cuisine filter stays
// ~20 options as new areas are added.
//
// ORDER MATTERS — first match wins, and several types contain other groups'
// keywords as substrings: latin_american ⊃ american, pizza_delivery ⊃ deli,
// steak ⊃ tea (hence tea_house, never bare "tea"), barbecue / oyster_bar ⊃
// bar. Keep the more specific group above the one it collides with.
const GROUPS = [
  ["Vegan & Health", ["vegan", "vegetarian", "health_food"]],
  ["Italian & Pizza", ["italian", "pizza"]],
  ["Japanese & Sushi", ["japanese", "sushi", "ramen"]],
  ["Chinese", ["chinese"]],
  ["Thai", ["thai"]],
  ["Vietnamese", ["vietnamese"]],
  ["Korean", ["korean"]],
  ["Indian", ["indian"]],
  ["Other Asian", ["asian", "malaysian", "filipino", "indonesian", "hawaiian", "poke"]],
  ["Mexican & Latin", [
    "mexican", "taco", "burrito", "tex_mex", "latin", "cuban", "peruvian",
    "colombian", "brazilian", "argentin", "venezuelan", "caribbean",
    "spanish", "tapas", "arepa", "empanada",
  ]],
  ["Mediterranean & Middle Eastern", [
    "mediterranean", "greek", "middle_eastern", "turkish", "persian",
    "lebanese", "israeli", "falafel", "kebab", "afghan",
  ]],
  ["Seafood", ["seafood", "oyster", "fish", "crab", "lobster"]],
  ["Barbecue & Southern", ["barbecue", "bbq", "cajun", "creole", "soul_food", "southern"]],
  ["American & Burgers", [
    "american", "steak", "hamburger", "burger", "hot_dog", "chicken",
    "wings", "diner", "cheesesteak",
  ]],
  ["Breakfast & Brunch", ["breakfast", "brunch", "pancake", "waffle"]],
  ["Sandwiches & Deli", ["sandwich", "deli", "bagel"]],
  ["Coffee & Tea", ["coffee", "cafe", "tea_house", "boba", "bubble_tea"]],
  ["Sweets & Treats", [
    "ice_cream", "dessert", "bakery", "cake", "donut", "doughnut",
    "chocolate", "candy", "confectionery", "frozen_yogurt", "gelato",
    "pastry", "creperie", "acai", "juice", "smoothie",
  ]],
  ["Bars & Breweries", [
    "bar", "pub", "brewery", "brewpub", "beer", "wine", "cocktail",
    "lounge", "taproom", "distillery",
  ]],
  ["Fast Food", ["fast_food", "food_court"]],
];

export function cuisineLabel(primaryType) {
  const type = (primaryType || "").toLowerCase();
  if (!type || type === "restaurant") return "Other";
  for (const [label, matches] of GROUPS) {
    if (matches.some((value) => type.includes(value))) return label;
  }
  // Generic and rare types (market, hotel, meal_takeaway, fine_dining…)
  // consolidate under Other rather than becoming their own filter option.
  return "Other";
}

// Venues whose product IS dessert. Their headline vegan count includes
// desserts (backend: db.DESSERT_VENUE_TYPES — keep the two lists in sync),
// and the UI labels those counts "vegan treats" instead of "vegan meals".
const DESSERT_VENUE_TYPES = new Set([
  "ice_cream_shop", "dessert_shop", "dessert_restaurant", "bakery",
  "donut_shop", "bagel_shop", "cake_shop", "pastry_shop",
  "chocolate_shop", "chocolate_factory", "candy_store", "confectionery",
  "frozen_yogurt_shop", "acai_shop",
]);

export function isDessertVenue(primaryType) {
  return DESSERT_VENUE_TYPES.has((primaryType || "").toLowerCase());
}

// Menu opening behavior is intentionally narrower than dessert scoring.
// Bagel/acai shops count their signature items correctly in headline totals,
// but their Food tab is usually the meaningful place to start. Bakeries are
// mixed, so their actual classified menu balance decides.
const DESSERT_FIRST_TYPES = new Set([
  "ice_cream_shop",
  "dessert_shop",
  "dessert_restaurant",
  "donut_shop",
  "cake_shop",
  "pastry_shop",
  "chocolate_shop",
  "chocolate_factory",
  "candy_store",
  "confectionery",
  "frozen_yogurt_shop",
]);

export function preferredMenuCategory(primaryType, counts = {}) {
  const food = Number(counts.food) || 0;
  const dessert = Number(counts.dessert) || 0;
  const drink = Number(counts.drink) || 0;
  const type = (primaryType || "").toLowerCase();
  if (
    dessert > 0 &&
    (DESSERT_FIRST_TYPES.has(type) ||
      (type === "bakery" && dessert >= food))
  ) {
    return "dessert";
  }
  if (food > 0) return "food";
  if (dessert > 0) return "dessert";
  if (drink > 0) return "drink";
  return "food";
}

const DESSERT_MAP_TYPES = new Set([
  ...DESSERT_FIRST_TYPES,
  "bakery",
  "acai_shop",
]);

const COFFEE_MAP_TYPES = new Set([
  "coffee_shop",
  "coffee_roastery",
  "coffee_stand",
  "cafe",
  "tea_house",
]);

export const VENUE_KIND_OPTIONS = Object.freeze([
  { key: "restaurant", label: "Restaurant", shortLabel: "Restaurant" },
  { key: "coffee", label: "Coffee / cafe", shortLabel: "Coffee" },
  { key: "dessert", label: "Dessert / treats", shortLabel: "Dessert" },
]);

export function venueKind(primaryType) {
  const type = (primaryType || "").toLowerCase();
  if (DESSERT_MAP_TYPES.has(type)) return "dessert";
  if (COFFEE_MAP_TYPES.has(type)) return "coffee";
  return "restaurant";
}

export function venueKindLabel(kind) {
  return (
    VENUE_KIND_OPTIONS.find((option) => option.key === kind)?.label ||
    "Restaurant"
  );
}

export function cuisineOptions(items) {
  return [...new Set(items.map((item) => cuisineLabel(item.primary_type)))].sort(
    (a, b) =>
      // "Other" is the catch-all bucket — it belongs at the end of the
      // dropdown, not alphabetized into the real cuisines.
      Number(a === "Other") - Number(b === "Other") || a.localeCompare(b)
  );
}
