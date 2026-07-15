// Global dish rows intentionally omit restaurant metadata. Rehydrate it from
// the much smaller restaurant directory, while retaining compatibility with
// older/full API rows when the directory has not arrived yet.
const RESTAURANT_FIELDS = [
  "address",
  "website_url",
  "lat",
  "lng",
  "place_id",
  "consumer_hidden",
  "archived",
  "serves_vegetarian",
  "price_level",
  "primary_type",
  "rating",
  "user_rating_count",
  "open_now",
  "opening_hours",
  "enriched_at",
  "menu_fetched_at",
  "business_status",
];

export function withRestaurantContext(dish, restaurant) {
  const hydrated = { ...dish };
  if (!restaurant) {
    hydrated.restaurant_name = dish.restaurant_name ?? "Restaurant";
    return hydrated;
  }
  hydrated.restaurant_name =
    restaurant.name ?? dish.restaurant_name ?? "Restaurant";
  for (const field of RESTAURANT_FIELDS) {
    // Directory data is canonical and, importantly, shares one hours array
    // across every dish from a restaurant.
    hydrated[field] = restaurant[field] ?? dish[field] ?? null;
  }
  return hydrated;
}
