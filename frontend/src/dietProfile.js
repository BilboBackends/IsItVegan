// One place that answers "how suitable is this dish for a dietary profile?"
// Vegan is the app's default and, for now, only surfaced profile; vegetarian
// and pescatarian read the second-pass enrichment fields so a future profile
// switcher is a settings toggle plus copy work, not a refactor. Anything that
// ranks, filters, or counts by diet should ask this module instead of
// re-deriving vegan-ness from raw verdicts.

export const DEFAULT_PROFILE = "vegan";

// Suitability tiers, best first — rank ascending. UNKNOWN sits above NO:
// "we can't tell" is still a better suggestion than "definitely not".
export const SUITS = 0;
export const LIKELY = 1;
export const ADAPTABLE = 2;
export const UNKNOWN = 3;
export const NO = 4;

const VEGAN_TIERS = {
  vegan: SUITS,
  likely_vegan: LIKELY,
  vegan_adaptable: ADAPTABLE,
  unclear: UNKNOWN,
  not_vegan: NO,
};

const FISH_MEATS = new Set(["fish", "shellfish"]);

export function suitabilityTier(dish, profile = DEFAULT_PROFILE) {
  if (!dish) return UNKNOWN;
  const vegan = VEGAN_TIERS[dish.verdict] ?? UNKNOWN;
  if (profile === "vegan") return vegan;

  let vegetarian;
  if (dish.vegetarian_status === "vegetarian") {
    vegetarian = SUITS;
  } else if (dish.vegetarian_status === "not_vegetarian") {
    vegetarian = NO;
  } else if (vegan === SUITS || vegan === LIKELY) {
    // Not enriched yet: every vegan-ish dish is vegetarian.
    vegetarian = vegan;
  } else if (vegan === ADAPTABLE) {
    // A dish can be vegan_adaptable BECAUSE of meat ("hold the bacon"), so
    // without an explicit vegetarian_status it only reaches LIKELY.
    vegetarian = LIKELY;
  } else {
    vegetarian = UNKNOWN;
  }
  if (profile === "vegetarian") return vegetarian;

  if (profile === "pescatarian") {
    const meats = dish.meat_sources || [];
    if (meats.some((meat) => !FISH_MEATS.has(meat))) return NO;
    if (meats.length) return SUITS; // only fish/shellfish named
    if (vegetarian === NO) return UNKNOWN; // meat implied but never named
    return vegetarian;
  }
  return vegan;
}
