import { useEffect, useState } from "react";

// Shared dish-verdict modal (used by both Explore and Admin). Fetches its own
// dishes for the given restaurant; every verdict shows confidence, reasoning,
// and the verbatim menu evidence — no verdict without evidence (CLAUDE.md).

export const VERDICT_STYLES = {
  vegan: "bg-emerald-100 text-emerald-800",
  likely_vegan: "bg-lime-100 text-lime-800",
  vegan_adaptable: "bg-amber-100 text-amber-800",
  unclear: "bg-slate-100 text-slate-500",
  not_vegan: "bg-rose-50 text-rose-700",
};

const FILTERS = [
  { key: "all", label: "All" },
  { key: "veganish", label: "Vegan options" },
  { key: "not_vegan", label: "Not vegan" },
];

const VEGANISH = new Set(["vegan", "likely_vegan", "vegan_adaptable"]);

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

function DishList({ items }) {
  return (
    <ul className="divide-y divide-slate-100">
      {items.map((d) => (
        <li key={d.id} className="py-3">
          <div className="flex items-baseline justify-between gap-3">
            <div className="font-medium text-slate-900">
              {d.name}
              {d.price && (
                <span className="ml-2 text-sm font-normal text-slate-400">
                  {d.price}
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
          {d.reasoning && (
            <div className="mt-1 text-xs text-slate-400">{d.reasoning}</div>
          )}
        </li>
      ))}
    </ul>
  );
}

function SectionHeading({ children }) {
  return (
    <h3 className="mb-1 border-b border-slate-200 pb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">
      {children}
    </h3>
  );
}

export default function DishModal({ restaurant, onClose }) {
  const [dishes, setDishes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("all");
  const [showDrinks, setShowDrinks] = useState(false);

  useEffect(() => {
    if (!restaurant) return;
    setLoading(true);
    setDishes([]);
    fetch(`/api/restaurants/${restaurant.id}/dishes`)
      .then((res) => (res.ok ? res.json() : { dishes: [] }))
      .then((data) => setDishes(data.dishes || []))
      .catch(() => setDishes([]))
      .finally(() => setLoading(false));
  }, [restaurant]);

  if (!restaurant) return null;

  const shown = dishes.filter((d) => {
    if (filter === "veganish") return VEGANISH.has(d.verdict);
    if (filter === "not_vegan") return !VEGANISH.has(d.verdict);
    return true;
  });

  // Group by category so a long cocktail list never buries the food.
  const food = shown.filter((d) => !d.category || d.category === "food");
  const desserts = shown.filter((d) => d.category === "dessert");
  const drinks = shown.filter((d) => d.category === "drink");

  return (
    <div
      className="fixed inset-0 z-10 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
          <h2 className="font-semibold text-slate-900">
            {restaurant.name}
            <span className="ml-2 text-sm font-normal text-slate-400">
              {restaurant.vegan_options} vegan option
              {restaurant.vegan_options === 1 ? "" : "s"} of{" "}
              {restaurant.dish_count} items
            </span>
          </h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            ✕
          </button>
        </div>

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

        <div className="overflow-y-auto p-4">
          {loading ? (
            <div className="text-slate-400">Loading…</div>
          ) : shown.length === 0 ? (
            <div className="text-slate-400">
              {dishes.length === 0
                ? "No dishes classified yet."
                : "No dishes match this filter."}
            </div>
          ) : (
            <div className="space-y-5">
              {food.length > 0 && (
                <section>
                  <SectionHeading>🍽 Food ({food.length})</SectionHeading>
                  <DishList items={food} />
                </section>
              )}
              {desserts.length > 0 && (
                <section>
                  <SectionHeading>🍰 Desserts ({desserts.length})</SectionHeading>
                  <DishList items={desserts} />
                </section>
              )}
              {drinks.length > 0 && (
                <section>
                  <SectionHeading>🥤 Drinks ({drinks.length})</SectionHeading>
                  {showDrinks ? (
                    <>
                      <DishList items={drinks} />
                      <button
                        onClick={() => setShowDrinks(false)}
                        className="mt-1 text-xs font-medium text-slate-500 hover:text-slate-700"
                      >
                        Hide drinks
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => setShowDrinks(true)}
                      className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
                    >
                      Show {drinks.length} drink{drinks.length === 1 ? "" : "s"}
                    </button>
                  )}
                </section>
              )}
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
