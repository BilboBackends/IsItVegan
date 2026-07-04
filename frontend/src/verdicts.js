// What earns a spot in a headline "N vegan" count. Mirrors
// db.STRICT_LIKELY_VEGAN_MIN_CONFIDENCE — keep the two in sync.
//
// Strict on purpose: `vegan_adaptable` ("ask them to hold the cheese")
// NEVER counts as vegan on a card, and `likely_vegan` only counts when the
// model was confident. Browsing filters can still show the softer verdicts —
// each dish wears its own chip — but a number that says "vegan" means it.
export const LIKELY_VEGAN_MIN_CONFIDENCE = 0.75;

export function isCountedVegan(dish) {
  if (!dish) return false;
  if (dish.verdict === "vegan") return true;
  return (
    dish.verdict === "likely_vegan" &&
    (dish.confidence ?? 0) >= LIKELY_VEGAN_MIN_CONFIDENCE
  );
}
