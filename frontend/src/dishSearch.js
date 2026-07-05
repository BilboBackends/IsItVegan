const VEGANISH = new Set(["vegan", "likely_vegan", "vegan_adaptable"]);

const PROTEIN_TERMS = [
  "tofu",
  "tempeh",
  "seitan",
  "lentil",
  "chickpea",
  "bean",
  "edamame",
  "egg",
  "chicken",
  "beef",
  "pork",
  "fish",
  "salmon",
  "tuna",
];

const MEAL_FALLBACKS = {
  breakfast: [
    "breakfast",
    "pancake",
    "waffle",
    "omelet",
    "omelette",
    "scramble",
    "oatmeal",
    "french toast",
    "benedict",
  ],
  brunch: ["brunch", "benedict", "mimosa"],
  lunch: ["lunch"],
  dinner: ["dinner", "supper"],
  snack: ["snack", "small plate", "appetizer", "starter"],
};

export function normalizeSearch(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function parseDishQuery(value) {
  let text = normalizeSearch(value);
  const intent = {
    veganFriendly: /\bvegan(?: friendly)?\b|\bplant based\b/.test(text),
    dairyFree: /\bdairy free\b|\bno dairy\b/.test(text),
    glutenFree: /\bgluten free\b|\bno gluten\b/.test(text),
    nutFree: /\bnut free\b|\bno nuts?\b/.test(text),
    highProtein: /\bhigh protein\b|\bprotein rich\b/.test(text),
    mealTypes: [],
    terms: [],
  };

  for (const meal of Object.keys(MEAL_FALLBACKS)) {
    if (new RegExp(`\\b${meal}\\b`).test(text)) intent.mealTypes.push(meal);
  }

  text = text
    .replace(/\b(?:vegan(?: friendly)?|plant based)\b/g, " ")
    .replace(/\b(?:dairy free|no dairy)\b/g, " ")
    .replace(/\b(?:gluten free|no gluten)\b/g, " ")
    .replace(/\b(?:nut free|no nuts?)\b/g, " ")
    .replace(/\b(?:high protein|protein rich)\b/g, " ");
  for (const meal of Object.keys(MEAL_FALLBACKS)) {
    text = text.replace(new RegExp(`\\b${meal}\\b`, "g"), " ");
  }
  intent.terms = text.trim().split(/\s+/).filter(Boolean);
  return intent;
}

function searchableText(dish) {
  return normalizeSearch(
    [
      dish.name,
      dish.raw_description,
      dish.calories,
      dish.restaurant_name,
      dish.primary_type,
      dish.address,
      dish.reasoning,
      ...(dish.key_ingredients || []),
      ...(dish.meal_types || []),
    ]
      .filter(Boolean)
      .join(" ")
  );
}

export function buildDishSearchIndex(dish) {
  return {
    haystack: searchableText(dish),
    name: normalizeSearch(dish.name),
    description: normalizeSearch(dish.raw_description),
    ingredients: normalizeSearch((dish.key_ingredients || []).join(" ")),
  };
}

function hasTerm(haystack, term) {
  if (haystack.includes(term)) return true;
  return term.length > 3 && term.endsWith("s") && haystack.includes(term.slice(0, -1));
}

function hasMealType(dish, meal, haystack) {
  if ((dish.meal_types || []).includes(meal)) return true;
  return (MEAL_FALLBACKS[meal] || []).some((term) => haystack.includes(term));
}

function looksHighProtein(dish, haystack) {
  if (dish.protein_level === "high") return true;
  if (dish.protein_level && dish.protein_level !== "unclear") return false;
  return PROTEIN_TERMS.some((term) => haystack.includes(term));
}

export function dishMatchesQuery(dish, intent, index = null) {
  const haystack = index?.haystack ?? searchableText(dish);
  if (intent.veganFriendly && !VEGANISH.has(dish.verdict)) return false;
  if (intent.dairyFree && dish.dairy_status !== "free") return false;
  if (intent.glutenFree && dish.gluten_status !== "free") return false;
  if (intent.nutFree && dish.nut_status !== "free") return false;
  if (intent.highProtein && !looksHighProtein(dish, haystack)) return false;
  if (intent.mealTypes.some((meal) => !hasMealType(dish, meal, haystack))) return false;
  return intent.terms.every((term) => hasTerm(haystack, term));
}

export function dishSearchScore(dish, rawQuery, intent, index = null) {
  const query = normalizeSearch(rawQuery);
  const name = index?.name ?? normalizeSearch(dish.name);
  const description = index?.description ?? normalizeSearch(dish.raw_description);
  const ingredients = index?.ingredients ?? normalizeSearch((dish.key_ingredients || []).join(" "));
  let score = 0;
  if (name === query) score += 100;
  else if (query && name.startsWith(query)) score += 70;
  else if (query && name.includes(query)) score += 50;
  for (const term of intent.terms) {
    if (name.includes(term)) score += 12;
    else if (ingredients.includes(term)) score += 9;
    else if (description.includes(term)) score += 5;
    else score += 1;
  }
  if (intent.veganFriendly && dish.verdict === "vegan") score += 4;
  if (intent.highProtein && dish.protein_level === "high") score += 4;
  return score;
}

export function queryIntentLabels(intent) {
  const labels = [];
  if (intent.veganFriendly) labels.push("vegan-friendly");
  if (intent.dairyFree) labels.push("dairy-free");
  if (intent.glutenFree) labels.push("gluten-free");
  if (intent.nutFree) labels.push("nut-free");
  if (intent.highProtein) labels.push("high protein");
  labels.push(...intent.mealTypes);
  return labels;
}
