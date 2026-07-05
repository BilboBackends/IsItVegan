import { useEffect, useMemo, useState } from "react";
import DietaryBadges from "./DietaryBadges.jsx";
import RatingBadge from "./RatingBadge.jsx";
import { FreshnessBadge, OpenStatusBadge } from "./RestaurantMeta.jsx";
import { calorieLabel } from "./calories.js";
import { isCountedVegan } from "./verdicts.js";

// Shared dish-verdict modal (used by both Explore and Admin). Fetches its own
// dishes for the given restaurant; every verdict shows confidence, reasoning,
// and the verbatim menu evidence — no verdict without evidence (CLAUDE.md).
// Items are split into category tabs so a long bar list never buries the food.

export const VERDICT_STYLES = {
  vegan: "bg-emerald-100 text-emerald-800",
  likely_vegan: "bg-lime-100 text-lime-800",
  vegan_adaptable: "bg-amber-100 text-amber-800",
  unclear: "bg-slate-100 text-slate-500",
  not_vegan: "bg-rose-50 text-rose-700",
};

const VEGANISH = new Set(["vegan", "likely_vegan", "vegan_adaptable"]);

const CATEGORIES = [
  { key: "food", label: "Food", icon: "🍽" },
  { key: "dessert", label: "Desserts", icon: "🍰" },
  { key: "drink", label: "Drinks", icon: "🥤" },
];

// The browse filter is deliberately WIDER than the strict counted-vegan
// standard: adaptable/likely dishes are worth seeing (each wears its own
// verdict chip) — they just don't get counted as vegan in the numbers.
const FILTERS = [
  { key: "all", label: "All" },
  { key: "veganish", label: "Vegan-friendly" },
  { key: "not_vegan", label: "Not vegan" },
];

export function VerdictChip({ verdict }) {
  if (!verdict) return <span className="text-xs text-slate-300">—</span>;
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-xs font-medium ${
        VERDICT_STYLES[verdict] || VERDICT_STYLES.unclear
      }`}
    >
      {verdict.replaceAll("_", " ")}
    </span>
  );
}

function dishCategory(d) {
  return d.category === "drink" || d.category === "dessert"
    ? d.category
    : "food";
}

export default function DishModal({ restaurant, onClose }) {
  const [dishes, setDishes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("food");
  const [filter, setFilter] = useState("all");
  const [servingFilter, setServingFilter] = useState("all");

  useEffect(() => {
    if (!restaurant) return;
    setLoading(true);
    setDishes([]);
    setFilter("all");
    setServingFilter("all");
    fetch(`/api/restaurants/${restaurant.id}/dishes`)
      .then((res) => (res.ok ? res.json() : { dishes: [] }))
      .then((data) => {
        const list = data.dishes || [];
        setDishes(list);
        // Open on the first category that actually has items.
        const first = CATEGORIES.find((c) =>
          list.some((d) => dishCategory(d) === c.key)
        );
        setTab(first ? first.key : "food");
      })
      .catch(() => setDishes([]))
      .finally(() => setLoading(false));
  }, [restaurant]);

  const byCategory = useMemo(() => {
    const groups = { food: [], dessert: [], drink: [] };
    for (const d of dishes) groups[dishCategory(d)].push(d);
    return groups;
  }, [dishes]);

  // Tab badges use the strict standard so they agree with the card counts.
  const veganishIn = (items) => items.filter(isCountedVegan).length;

  if (!restaurant) return null;

  const active = byCategory[tab] || [];
  const verdictShown = active.filter((d) => {
    if (filter === "veganish") return VEGANISH.has(d.verdict);
    if (filter === "not_vegan") return !VEGANISH.has(d.verdict);
    return true;
  });
  const mealItems = verdictShown.filter((dish) => dish.serving_role !== "side");
  const sideItems = verdictShown.filter((dish) => dish.serving_role === "side");
  const shown =
    tab !== "food" || servingFilter === "all"
      ? verdictShown
      : servingFilter === "side"
        ? sideItems
        : mealItems;
  const displayGroups =
    tab === "food"
      ? [
          servingFilter !== "side" && {
            key: "meal",
            label: "Full meals",
            description: "Substantial dishes intended as a main.",
            items: mealItems,
          },
          servingFilter !== "meal" && {
            key: "side",
            label: "Sides & small plates",
            description: "Accompaniments, snacks, starters, and smaller plates.",
            items: sideItems,
          },
        ].filter(Boolean)
      : [{ key: tab, label: null, description: null, items: shown }];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
          <div>
            <h2 className="font-semibold text-slate-900">{restaurant.name}</h2>
            <div className="mt-0.5 flex flex-wrap items-center gap-2">
              <span className="text-sm font-normal text-slate-400">
                {restaurant.vegan_options} vegan meal
                {restaurant.vegan_options === 1 ? "" : "s"}
                {restaurant.vegan_sides > 0 &&
                  ` · ${restaurant.vegan_sides} side${
                    restaurant.vegan_sides === 1 ? "" : "s"
                  }`}
              </span>
              <RatingBadge
                rating={restaurant.rating}
                userRatingCount={restaurant.user_rating_count}
              />
              <OpenStatusBadge
                openNow={restaurant.open_now}
                enrichedAt={restaurant.enriched_at}
              />
              <FreshnessBadge fetchedAt={restaurant.menu_fetched_at} compact />
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            ✕
          </button>
        </div>

        {/* Category tabs */}
        <div className="flex gap-1 border-b border-slate-200 px-4 pt-2">
          {CATEGORIES.map((c) => {
            const items = byCategory[c.key];
            if (!items || items.length === 0) return null;
            const veg = veganishIn(items);
            return (
              <button
                key={c.key}
                onClick={() => {
                  setTab(c.key);
                  setServingFilter("all");
                }}
                className={`relative -mb-px rounded-t-lg border px-3 py-2 text-sm font-medium transition ${
                  tab === c.key
                    ? "border-slate-200 border-b-white bg-white text-slate-900"
                    : "border-transparent text-slate-500 hover:text-slate-800"
                }`}
              >
                {c.icon} {c.label}
                <span className="ml-1.5 text-xs text-slate-400">
                  {items.length}
                </span>
                {veg > 0 && (
                  <span className="ml-1.5 rounded-full bg-emerald-100 px-1.5 text-xs font-semibold text-emerald-700">
                    {veg}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Verdict filter */}
        <div className="flex gap-2 border-b border-slate-100 px-4 py-2">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                filter === f.key
                  ? "bg-emerald-600 text-white"
                  : "bg-slate-100 text-slate-600 hover:bg-slate-200"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {tab === "food" && (
          <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 bg-slate-50/70 px-4 py-2">
            <span className="mr-1 text-[11px] font-bold uppercase tracking-wide text-slate-400">
              Show
            </span>
            {[
              ["all", "All food", verdictShown.length],
              ["meal", "Full meals", mealItems.length],
              ["side", "Sides & small plates", sideItems.length],
            ].map(([key, label, count]) => (
              <button
                key={key}
                onClick={() => setServingFilter(key)}
                className={`rounded-full px-3 py-1 text-xs font-semibold transition ${
                  servingFilter === key
                    ? "bg-slate-800 text-white"
                    : "border border-slate-200 bg-white text-slate-600 hover:border-slate-400"
                }`}
              >
                {label} <span className="ml-1 opacity-70">{count}</span>
              </button>
            ))}
          </div>
        )}

        <div className="overflow-y-auto p-4">
          {loading ? (
            <div className="text-slate-400">Loading…</div>
          ) : dishes.length === 0 ? (
            <div className="text-slate-400">No dishes classified yet.</div>
          ) : shown.length === 0 ? (
            <div className="text-slate-400">
              No {tab === "food" ? "food items" : tab + "s"} match this filter.
            </div>
          ) : (
            <div className="space-y-5">
              {displayGroups.map((group) => {
                if (group.items.length === 0) return null;
                return (
                  <section key={group.key}>
                    {group.label && (
                      <div className="mb-1 flex items-end justify-between gap-3 border-b border-slate-200 pb-2">
                        <div>
                          <h3 className="text-sm font-bold text-slate-800">{group.label}</h3>
                          <p className="text-xs text-slate-400">{group.description}</p>
                        </div>
                        <span className="shrink-0 text-xs font-semibold text-slate-400">
                          {group.items.length} item{group.items.length === 1 ? "" : "s"}
                        </span>
                      </div>
                    )}
                    <ul className="divide-y divide-slate-100">
              {group.items.map((d) => (
                <li key={d.id} className="py-3 first:pt-2">
                  <div className="flex items-baseline justify-between gap-3">
                    <div className="font-medium text-slate-900">
                      {d.name}
                      {d.price && (
                        <span className="ml-2 text-sm font-normal text-slate-400">
                          {d.price}
                        </span>
                      )}
                      {d.calories && (
                        <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-500">
                          {calorieLabel(d.calories)}
                        </span>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <VerdictChip verdict={d.verdict} />
                      {d.confidence != null && (
                        <span className="text-xs text-slate-400">
                          {Math.round(d.confidence * 100)}%
                        </span>
                      )}
                    </div>
                  </div>
                  {d.raw_description && (
                    <div className="mt-0.5 text-sm text-slate-500">
                      {d.raw_description}
                    </div>
                  )}
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    <DietaryBadges dish={d} maxBadges={3} />
                  </div>
                  {d.reasoning && (
                    <div className="mt-1 text-xs text-slate-400">{d.reasoning}</div>
                  )}
                  <a
                    href={`#dishes?dish=${d.id}`}
                    className="mt-1 inline-block text-xs font-bold text-emerald-700 hover:underline"
                  >
                    Details, share, or report →
                  </a>
                </li>
              ))}
                    </ul>
                  </section>
                );
              })}
            </div>
          )}
        </div>
        <div className="border-t border-slate-200 px-4 py-2 text-xs text-slate-400">
          Verdicts are inferred from the restaurant's menu text — evidence shown
          under each dish. Always confirm with the restaurant for allergies or
          strict diets.
        </div>
      </div>
    </div>
  );
}
