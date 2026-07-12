import { useEffect, useMemo, useState } from "react";
import DietaryBadges from "./DietaryBadges.jsx";
import RatingBadge from "./RatingBadge.jsx";
import { FreshnessBadge, OpenStatusBadge, TodayHours } from "./RestaurantMeta.jsx";
import { calorieLabel } from "./calories.js";
import { isDessertVenue } from "./cuisine.js";
import { isCountedVegan } from "./verdicts.js";
import { fetchRestaurantDishes } from "./staticData.js";
import {
  CLOUD_ENABLED,
  fetchComments,
  registerDishes,
  registerRestaurants,
} from "./cloud.js";
import Comments from "./Comments.jsx";
import ThumbVote from "./ThumbVote.jsx";

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

// Most-vegan-first display order. The backend endpoint already sorts this
// way, but the static site derives dishes from an alphabetical snapshot —
// sorting here guarantees the order regardless of data source.
const VERDICT_RANK = {
  vegan: 0,
  likely_vegan: 1,
  vegan_adaptable: 2,
  unclear: 3,
  not_vegan: 4,
};

function byMostVegan(a, b) {
  return (
    (VERDICT_RANK[a.verdict] ?? 5) - (VERDICT_RANK[b.verdict] ?? 5) ||
    (b.confidence ?? 0) - (a.confidence ?? 0) ||
    a.name.localeCompare(b.name)
  );
}

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

export default function DishModal({
  restaurant,
  onClose,
  onOpenDish,
  // "veganish" opens the menu pre-filtered to vegan-friendly items — the
  // card's vegan-count text uses this as a direct shortcut.
  initialFilter = "all",
  // "comments" jumps straight to the Tips tab (the card's 💬 chip).
  initialTab = null,
}) {
  const [dishes, setDishes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("food");
  // The restaurant's comment thread lives up here so the Tips tab can show
  // its count before the pane is opened.
  const [comments, setComments] = useState(null);
  const [filter, setFilter] = useState("all");
  const [servingFilter, setServingFilter] = useState("all");
  // Phones only: the two filter rows start collapsed behind a "Filters"
  // disclosure so the dish list gets the space; desktop always shows them.
  const [filtersOpen, setFiltersOpen] = useState(false);
  // Section disclosure (Full meals / Sides, Soft drinks / Alcoholic):
  // everything starts OPEN; tapping a header collapses just that section.
  const [collapsed, setCollapsed] = useState(() => new Set());

  function toggleGroup(key) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  useEffect(() => {
    if (!restaurant) return;
    setLoading(true);
    setDishes([]);
    setFilter(initialFilter);
    setServingFilter("all");
    setFiltersOpen(false);
    setCollapsed(new Set());
    fetchRestaurantDishes(restaurant.id)
      .then((res) => (res.ok ? res.json() : { dishes: [] }))
      .then((data) => {
        const list = data.dishes || [];
        setDishes(list);
        // Stable identities for account features (likes key on place_id +
        // dish name, not the renumber-prone numeric ids).
        registerRestaurants([restaurant]);
        registerDishes(list);
        // Open on the first category that actually has items — unless the
        // caller asked for the Tips tab directly (card's 💬 chip).
        const first = CATEGORIES.find((c) =>
          list.some((d) => dishCategory(d) === c.key)
        );
        setTab(
          initialTab === "comments" ? "comments" : first ? first.key : "food"
        );
      })
      .catch(() => setDishes([]))
      .finally(() => setLoading(false));
  }, [restaurant, initialFilter, initialTab]);

  // Load the thread alongside the dishes so the Tips tab shows its count.
  useEffect(() => {
    setComments(null);
    if (!CLOUD_ENABLED || !restaurant?.place_id) return;
    let cancelled = false;
    fetchComments(restaurant.place_id)
      .then((rows) => !cancelled && setComments(rows))
      .catch(() => !cancelled && setComments([]));
    return () => {
      cancelled = true;
    };
  }, [restaurant]);

  useEffect(() => {
    if (!restaurant) return;
    const closeOnEscape = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [restaurant, onClose]);

  const byCategory = useMemo(() => {
    const groups = { food: [], dessert: [], drink: [] };
    for (const d of dishes) groups[dishCategory(d)].push(d);
    for (const list of Object.values(groups)) list.sort(byMostVegan);
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
  // Drinks split the bar list from soft drinks — a Coke and a tequila are
  // not the same kind of "drink". Anything classified before the
  // alcohol_status attribute existed lands in "Uncategorized".
  const softDrinks = verdictShown.filter(
    (d) => d.alcohol_status === "non_alcoholic"
  );
  const alcoholicDrinks = verdictShown.filter(
    (d) => d.alcohol_status === "alcoholic"
  );
  const unlabeledDrinks = verdictShown.filter(
    (d) => d.alcohol_status !== "non_alcoholic" && d.alcohol_status !== "alcoholic"
  );
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
      : tab === "drink"
        ? [
            {
              key: "soft",
              label: "Soft drinks & non-alcoholic",
              description: "Sodas, juices, coffee, tea, smoothies, mocktails.",
              items: softDrinks,
            },
            {
              key: "alcohol",
              label: "Alcoholic",
              description: "Beer, wine, cocktails, and spirits.",
              items: alcoholicDrinks,
            },
            unlabeledDrinks.length > 0 && {
              key: "unlabeled",
              label: "Uncategorized drinks",
              description: "Classified before alcohol labeling existed.",
              items: unlabeledDrinks,
            },
          ].filter(Boolean)
        : [{ key: tab, label: null, description: null, items: shown }];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={`restaurant-menu-title-${restaurant.id}`}
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
          <div>
            <h2
              id={`restaurant-menu-title-${restaurant.id}`}
              className="font-semibold text-slate-900"
            >
              {restaurant.name}
            </h2>
            <div className="mt-0.5 flex flex-wrap items-center gap-2">
              <span className="text-sm font-normal text-slate-400">
                {restaurant.vegan_options} vegan{" "}
                {isDessertVenue(restaurant.primary_type) ? "treat" : "meal"}
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
                openingHours={restaurant.opening_hours}
              />
              <FreshnessBadge fetchedAt={restaurant.menu_fetched_at} compact />
            </div>
            <TodayHours
              openingHours={restaurant.opening_hours}
              className="mt-1 block text-xs font-medium text-slate-500"
            />
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={`Close ${restaurant.name} menu`}
            className="text-slate-400 hover:text-slate-700"
          >
            ✕
          </button>
        </div>

        {/* Category tabs. Tighter on phones and scrollable on overflow so a
            big drinks count (Anh Hong's 36) can't be clipped off the edge. */}
        <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-slate-200 px-4 pt-2">
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
                className={`relative -mb-px shrink-0 whitespace-nowrap rounded-t-lg border px-2 py-2 text-xs font-medium transition sm:px-3 sm:text-sm ${
                  tab === c.key
                    ? "border-slate-200 border-b-white bg-white text-slate-900"
                    : "border-transparent text-slate-500 hover:text-slate-800"
                }`}
              >
                {c.icon}
                <span className="max-sm:hidden"> {c.label}</span>
                <span className="ml-1 text-xs text-slate-400 sm:ml-1.5">
                  {items.length}
                </span>
                {veg > 0 && (
                  <span className="ml-1 rounded-full bg-emerald-100 px-1.5 text-xs font-semibold text-emerald-700 sm:ml-1.5">
                    {veg}
                  </span>
                )}
              </button>
            );
          })}
          {CLOUD_ENABLED && restaurant.place_id && (
            <button
              onClick={() => setTab("comments")}
              className={`relative -mb-px shrink-0 whitespace-nowrap rounded-t-lg border px-2 py-2 text-xs font-medium transition sm:px-3 sm:text-sm ${
                tab === "comments"
                  ? "border-slate-200 border-b-white bg-white text-slate-900"
                  : "border-transparent text-slate-500 hover:text-slate-800"
              }`}
            >
              💬<span className="max-sm:hidden"> Buzz</span>
              {(comments?.length ?? 0) > 0 && (
                <span className="ml-1 rounded-full bg-sky-100 px-1.5 text-xs font-semibold text-sky-700 sm:ml-1.5">
                  {comments.length}
                </span>
              )}
            </button>
          )}
        </div>

        {/* Mobile-only disclosure for the filter rows; the active count
            keeps collapsed filters from being forgotten. */}
        {tab !== "comments" && (
        <button
          onClick={() => setFiltersOpen((v) => !v)}
          className="flex w-full items-center justify-between border-b border-slate-100 px-4 py-2 text-xs font-semibold text-slate-600 sm:hidden"
        >
          <span>
            Filters
            {(filter !== "all" ? 1 : 0) +
              (tab === "food" && servingFilter !== "all" ? 1 : 0) >
              0 &&
              ` · ${
                (filter !== "all" ? 1 : 0) +
                (tab === "food" && servingFilter !== "all" ? 1 : 0)
              } active`}
          </span>
          <span className="text-slate-400">{filtersOpen ? "hide ▴" : "show ▾"}</span>
        </button>
        )}

        {/* Verdict filter */}
        {tab !== "comments" && (
        <div className={`flex gap-2 border-b border-slate-100 px-4 py-2 ${filtersOpen ? "" : "max-sm:hidden"}`}>
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
        )}

        {tab === "food" && (
          <div className={`flex flex-wrap items-center gap-2 border-b border-slate-100 bg-slate-50/70 px-4 py-2 ${filtersOpen ? "" : "max-sm:hidden"}`}>
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
          {tab === "comments" ? (
            <Comments
              restaurant={restaurant}
              dishes={dishes}
              onOpenDish={onOpenDish}
              comments={comments}
              onCommentsChange={setComments}
            />
          ) : loading ? (
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
                const isCollapsed = collapsed.has(group.key);
                return (
                  <section key={group.key}>
                    {group.label && (
                      <button
                        type="button"
                        onClick={() => toggleGroup(group.key)}
                        aria-expanded={!isCollapsed}
                        className="mb-1 flex w-full items-end justify-between gap-3 border-b border-slate-200 pb-2 text-left"
                      >
                        <div>
                          <h3 className="text-sm font-bold text-slate-800">{group.label}</h3>
                          {!isCollapsed && (
                            <p className="text-xs text-slate-400">{group.description}</p>
                          )}
                        </div>
                        <span className="shrink-0 text-xs font-semibold text-slate-400">
                          {group.items.length} item{group.items.length === 1 ? "" : "s"}{" "}
                          <span aria-hidden="true">{isCollapsed ? "▸" : "▾"}</span>
                        </span>
                      </button>
                    )}
                    {group.label && isCollapsed ? null : (
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
                      <ThumbVote
                        dishId={d.id}
                        upVotes={d.up_votes}
                        downVotes={d.down_votes}
                      />
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
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                    {onOpenDish ? (
                      // Host provides its own detail panel — stay on the
                      // current tab instead of navigating to Dishes.
                      <button
                        onClick={() => onOpenDish(d)}
                        className="font-bold text-emerald-700 hover:underline"
                      >
                        Details, share, or report →
                      </button>
                    ) : (
                      <a
                        href={`#dishes?dish=${d.id}`}
                        className="font-bold text-emerald-700 hover:underline"
                      >
                        Details, share, or report →
                      </a>
                    )}
                    {d.menu_url?.startsWith("http") && (
                      <a
                        href={d.menu_url}
                        target="_blank"
                        rel="noreferrer"
                        className="font-semibold text-stone-500 hover:text-emerald-700 hover:underline"
                      >
                        Source menu ↗
                      </a>
                    )}
                  </div>
                </li>
              ))}
                    </ul>
                    )}
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
