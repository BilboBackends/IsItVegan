// Price helpers shared by the dish and restaurant views.

// Menu price text is messy: "$12.65", "12.00", "$9 / $13 (sm/lg)",
// "Market Price", "Call for pricing". Returns the FIRST number found —
// the base price — or null when there's nothing numeric to filter on.
export function parsePriceValue(text) {
  if (!text) return null;
  const match = String(text).match(/\d{1,3}(?:\.\d{1,2})?/);
  return match ? parseFloat(match[0]) : null;
}

// Google Places price levels, cheapest to priciest.
const PRICE_LEVEL_RANKS = {
  PRICE_LEVEL_INEXPENSIVE: 1,
  PRICE_LEVEL_MODERATE: 2,
  PRICE_LEVEL_EXPENSIVE: 3,
  PRICE_LEVEL_VERY_EXPENSIVE: 4,
};

export function priceLevelRank(level) {
  return PRICE_LEVEL_RANKS[level] ?? null;
}

// "$" / "$$" / "$$$" / "$$$$", or null when Google doesn't know.
export function priceLevelSymbol(level) {
  const rank = priceLevelRank(level);
  return rank ? "$".repeat(rank) : null;
}
