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

export function cuisineOptions(items) {
  return [...new Set(items.map((item) => cuisineLabel(item.primary_type)))].sort(
    (a, b) => a.localeCompare(b)
  );
}
