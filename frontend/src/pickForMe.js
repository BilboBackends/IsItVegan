/**
 * "Pick for me" — a small recommender over the already-loaded restaurant list.
 *
 * Deliberately not a ranker: the Restaurants tab already sorts by vegan score,
 * so returning the top row every time would be useless. This weights a
 * shortlist of genuinely good options and samples from it, so pressing the
 * button twice suggests two different places.
 *
 * Every pick carries a `reason` built from the same vegan_score_parts the
 * score badge uses — a recommendation without evidence would violate the
 * "never show a verdict without showing why" rule the rest of the app follows.
 */

import { cuisineLabel, isDessertVenue } from "./cuisine.js";

const MIN_SCORE = 5;
const SHORTLIST = 25;

function priceTier(level) {
  switch (level) {
    case "PRICE_LEVEL_INEXPENSIVE":
      return 1;
    case "PRICE_LEVEL_MODERATE":
      return 2;
    case "PRICE_LEVEL_EXPENSIVE":
      return 3;
    case "PRICE_LEVEL_VERY_EXPENSIVE":
      return 4;
    default:
      return null;
  }
}

/** Candidates worth recommending at all. */
function isEligible(restaurant) {
  if (!restaurant || restaurant.archived || restaurant.consumer_hidden) return false;
  if (restaurant.business_status && restaurant.business_status !== "OPERATIONAL") {
    return false;
  }
  // A pick with nothing vegan to eat is not a pick.
  const options = (restaurant.vegan_options || 0) + (restaurant.vegan_sides || 0);
  return options > 0 && (restaurant.vegan_score || 0) >= MIN_SCORE;
}

/**
 * Why this restaurant — its most DISTINCTIVE signal, in plain words.
 *
 * Not a fixed priority list: everything on the shortlist has a big menu by
 * construction, so leading with selection made 90%+ of picks read "one of the
 * widest selections" and told the user nothing. Each candidate reason scores
 * how unusual it is for this restaurant, and the strongest one wins.
 *
 * Every branch cites a number straight off the restaurant record, so the copy
 * can't drift from the score badge beside it.
 */
export function pickReason(restaurant) {
  const meals = restaurant.vegan_options || 0;
  const sides = restaurant.vegan_sides || 0;
  const parts = restaurant.vegan_score_parts || {};
  const rating = restaurant.rating;
  const count = restaurant.user_rating_count || 0;
  // Dessert venues count treats, not meals — same wording the score badge
  // uses, so an ice cream shop never advertises "81 vegan meals".
  const dessert = isDessertVenue(restaurant.primary_type);
  const mealWord = dessert
    ? meals === 1
      ? "treat"
      : "treats"
    : meals === 1
      ? "meal"
      : "meals";
  const mealWordPlural = dessert ? "treats" : "meals";
  const candidates = [];

  // Beloved: a strong crowd rating with enough reviews to mean something.
  if (rating >= 4.6 && count >= 400) {
    candidates.push({
      weight: 3 + Math.min(2, count / 2000),
      text: `Rated ${rating} across ${count.toLocaleString()} reviews, with ${meals} vegan ${mealWord} on the menu.`,
    });
  }
  // Genuinely huge selection — only remarkable well above the shortlist norm.
  if (meals >= 20) {
    candidates.push({
      weight: 3.5,
      text: `${meals} vegan ${mealWordPlural} to choose from — one of the widest selections we've found.`,
    });
  } else if (meals >= 8) {
    candidates.push({
      weight: 1.5,
      text: `${meals} vegan ${mealWordPlural} on the menu.`,
    });
  }
  // Real plates rather than a pile of sides — meaningless at a dessert venue.
  if (parts.substance >= 1.5 && meals > 0 && !dessert) {
    candidates.push({
      weight: 2.5,
      text: `${meals} vegan ${mealWord} that read as full plates, not just sides.`,
    });
  }
  // A cuisine worth naming. Uses the curated cuisineLabel groups rather than
  // the raw Google primary_type — "chicken_restaurant" would otherwise render
  // as "this chicken spot", which reads terribly on a vegan app.
  const cuisine = cuisineLabel(restaurant.primary_type);
  if (cuisine !== "Other" && meals >= 4) {
    candidates.push({
      weight: 2,
      text: `${cuisine} — ${meals} vegan ${mealWord} on the menu.`,
    });
  }
  // Cheap eats.
  if (restaurant.price_level === "PRICE_LEVEL_INEXPENSIVE" && meals > 0) {
    candidates.push({
      weight: 2.2,
      text: `${meals} vegan ${mealWord}, and it's one of the cheaper places on the list.`,
    });
  }
  // Sides matter when the mains are thin.
  if (meals > 0 && sides >= 3 && meals < 8) {
    candidates.push({
      weight: 2,
      text: `${meals} vegan ${mealWord} plus ${sides} sides to build around.`,
    });
  }

  if (candidates.length === 0) {
    if (meals > 0) return `${meals} vegan ${mealWord} on the menu.`;
    return `${sides} vegan ${sides === 1 ? "side" : "sides"} on the menu.`;
  }
  // Jitter breaks ties so a restaurant doesn't always explain itself the
  // same way, without ever letting a weak reason beat a strong one.
  return candidates
    .map((candidate) => ({ ...candidate, weight: candidate.weight + Math.random() * 0.9 }))
    .sort((a, b) => b.weight - a.weight)[0].text;
}

/**
 * Weighted-random pick from the best `SHORTLIST` eligible restaurants.
 *
 * `filters.openNow` and `filters.maxPrice` let the caller respect what the
 * user already narrowed to; `excludeId` avoids handing back the same place
 * twice in a row.
 */
export function pickForMe(restaurants, { openNow = false, maxPrice = null, excludeId = null } = {}) {
  const eligible = (restaurants || []).filter((restaurant) => {
    if (!isEligible(restaurant)) return false;
    if (excludeId != null && restaurant.id === excludeId) return false;
    if (openNow && !restaurant.open_now) return false;
    if (maxPrice != null) {
      // Unpriced places (tier null) pass: Google simply has no price_level
      // for many restaurants, and silently hiding them would shrink the pool
      // more than the user asked for.
      const tier = priceTier(restaurant.price_level);
      if (tier != null && tier > maxPrice) return false;
    }
    return true;
  });
  if (eligible.length === 0) return null;

  const shortlist = eligible
    .slice()
    .sort((a, b) => (b.vegan_score || 0) - (a.vegan_score || 0))
    .slice(0, SHORTLIST);

  // Weight by score so strong places come up more often, without ever making
  // the pick deterministic.
  const weights = shortlist.map((restaurant) => {
    const score = restaurant.vegan_score || 0;
    return Math.max(0.1, score - MIN_SCORE + 1) ** 2;
  });
  const total = weights.reduce((sum, weight) => sum + weight, 0);

  let roll = Math.random() * total;
  for (let i = 0; i < shortlist.length; i += 1) {
    roll -= weights[i];
    if (roll <= 0) return shortlist[i];
  }
  return shortlist[shortlist.length - 1];
}
