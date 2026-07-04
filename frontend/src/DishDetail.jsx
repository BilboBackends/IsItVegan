import { useState } from "react";
import DietaryBadges, { DietaryProfile } from "./DietaryBadges.jsx";
import FavoriteButton from "./FavoriteButton.jsx";
import RatingBadge from "./RatingBadge.jsx";
import { FreshnessBadge, OpenStatusBadge } from "./RestaurantMeta.jsx";
import { VerdictChip } from "./DishModal.jsx";

const ISSUES = [
  ["animal_ingredient", "Contains an animal ingredient"],
  ["dish_removed", "Dish is no longer available"],
  ["wrong_restaurant", "Wrong restaurant or location"],
  ["other", "Something else"],
];

function splitReasoning(value) {
  if (!value) return { reasoning: null, evidence: null };
  const marker = " | evidence: ";
  const index = value.indexOf(marker);
  if (index === -1) return { reasoning: value, evidence: null };
  return {
    reasoning: value.slice(0, index),
    evidence: value.slice(index + marker.length),
  };
}

export default function DishDetail({
  dish,
  onClose,
  onShowMap,
  favorite,
  onToggleFavorite,
  restaurantFavorite,
  onToggleRestaurant,
}) {
  const [reportOpen, setReportOpen] = useState(false);
  const [issueType, setIssueType] = useState("animal_ingredient");
  const [note, setNote] = useState("");
  const [reportState, setReportState] = useState(null);
  const [copied, setCopied] = useState(false);
  if (!dish) return null;

  const details = splitReasoning(dish.reasoning);

  async function share() {
    const url = window.location.href;
    try {
      if (navigator.share) {
        await navigator.share({ title: `${dish.name} at ${dish.restaurant_name}`, url });
      } else {
        await navigator.clipboard.writeText(url);
        setCopied(true);
      }
    } catch {
      // A cancelled native share is not an error the user needs to see.
    }
  }

  async function submitReport(event) {
    event.preventDefault();
    setReportState("sending");
    try {
      const response = await fetch("/api/reports", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          restaurant_id: dish.restaurant_id,
          dish_id: dish.id,
          issue_type: issueType,
          note,
        }),
      });
      if (!response.ok) throw new Error("Report failed");
      setReportState("sent");
    } catch {
      setReportState("error");
    }
  }

  return (
    <div className="fixed inset-0 z-[1000] flex justify-end bg-stone-950/40" onClick={onClose}>
      <aside
        className="flex h-full w-full max-w-xl flex-col bg-[#faf8f4] shadow-2xl"
        onClick={(event) => event.stopPropagation()}
        aria-label={`${dish.name} details`}
      >
        <div className="flex items-start justify-between gap-4 border-b border-stone-200 bg-white px-5 py-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-extrabold text-stone-900">{dish.name}</h2>
              {dish.price && <span className="font-semibold text-stone-400">{dish.price}</span>}
            </div>
            <div className="mt-1 flex items-center gap-2 text-sm font-semibold text-emerald-800">
              {dish.restaurant_name}
              <button
                onClick={onToggleRestaurant}
                className="text-xs font-bold text-rose-600 hover:underline"
              >
                {restaurantFavorite ? "♥ Restaurant saved" : "♡ Save restaurant"}
              </button>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <FavoriteButton active={favorite} onClick={onToggleFavorite} label="dish" />
            <button onClick={onClose} className="h-8 w-8 rounded-full text-xl text-stone-400 hover:bg-stone-100 hover:text-stone-700" aria-label="Close details">
              ×
            </button>
          </div>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto p-5">
          <div className="flex flex-wrap items-center gap-2">
            <VerdictChip verdict={dish.verdict} />
            <DietaryBadges dish={dish} includeMeals />
            {dish.confidence != null && (
              <span className="text-xs font-semibold text-stone-500">{Math.round(dish.confidence * 100)}% confidence</span>
            )}
            {dish.distance != null && <span className="text-xs text-stone-500">{dish.distance.toFixed(1)} mi away</span>}
          </div>

          {dish.raw_description && (
            <section className="rounded-2xl border border-stone-200 bg-white p-4">
              <h3 className="text-xs font-bold uppercase tracking-wide text-stone-400">Menu description</h3>
              <p className="mt-1 text-sm leading-relaxed text-stone-700">{dish.raw_description}</p>
            </section>
          )}

          {(details.reasoning || details.evidence) && (
            <section className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
              <h3 className="text-xs font-bold uppercase tracking-wide text-emerald-800">Why this verdict</h3>
              {details.reasoning && <p className="mt-1 text-sm text-emerald-950">{details.reasoning}</p>}
              {details.evidence && (
                <blockquote className="mt-2 border-l-2 border-emerald-400 pl-3 text-sm italic text-emerald-900">
                  {details.evidence}
                </blockquote>
              )}
            </section>
          )}

          <section className="rounded-2xl border border-stone-200 bg-white p-4">
            <h3 className="text-xs font-bold uppercase tracking-wide text-stone-400">
              Dietary profile
            </h3>
            <div className="mt-3">
              <DietaryProfile dish={dish} />
            </div>
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-sm text-stone-600">
              {dish.protein_level && (
                <span><strong>Protein:</strong> {dish.protein_level}</span>
              )}
              {dish.meal_types?.length > 0 && (
                <span><strong>Meals:</strong> {dish.meal_types.join(", ")}</span>
              )}
            </div>
            {dish.key_ingredients?.length > 0 && (
              <p className="mt-2 text-sm text-stone-600">
                <strong>Key ingredients:</strong> {dish.key_ingredients.join(", ")}
              </p>
            )}
            <p className="mt-3 text-xs leading-relaxed text-stone-400">
              Inferred from menu text and common preparation. This does not certify
              allergy safety or cross-contact controls—confirm strict needs directly.
            </p>
          </section>

          <section className="rounded-2xl border border-stone-200 bg-white p-4">
            <div className="flex flex-wrap items-center gap-2">
              <RatingBadge rating={dish.rating} userRatingCount={dish.user_rating_count} />
              <OpenStatusBadge openNow={dish.open_now} enrichedAt={dish.enriched_at} />
              <FreshnessBadge fetchedAt={dish.menu_fetched_at} />
            </div>
            {dish.address && <p className="mt-2 text-sm text-stone-500">{dish.address}</p>}
            {dish.opening_hours?.length > 0 && (
              <details className="mt-3 text-sm">
                <summary className="cursor-pointer font-bold text-stone-700">Weekly opening hours</summary>
                <ul className="mt-2 space-y-1 text-xs text-stone-500">
                  {dish.opening_hours.map((line) => <li key={line}>{line}</li>)}
                </ul>
              </details>
            )}
            <div className="mt-3 flex flex-wrap gap-3 text-sm font-bold">
              {dish.lat != null && dish.lng != null && (
                <button onClick={() => onShowMap(dish)} className="text-emerald-700 hover:underline">Show on VeganFind map</button>
              )}
              {dish.website_url && (
                <a href={dish.website_url} target="_blank" rel="noreferrer" className="text-stone-600 hover:underline">Restaurant website ↗</a>
              )}
              {dish.menu_url?.startsWith("http") && (
                <a href={dish.menu_url} target="_blank" rel="noreferrer" className="text-stone-600 hover:underline">Source menu ↗</a>
              )}
            </div>
          </section>

          <div className="flex flex-wrap gap-2">
            <button onClick={share} className="rounded-full border border-stone-300 bg-white px-4 py-2 text-sm font-bold text-stone-700 hover:border-emerald-600">
              {copied ? "Link copied" : "Share dish"}
            </button>
            <button onClick={() => setReportOpen((value) => !value)} className="rounded-full px-4 py-2 text-sm font-semibold text-stone-500 hover:bg-stone-100">
              Report a problem
            </button>
          </div>

          {reportOpen && (
            <form onSubmit={submitReport} className="rounded-2xl border border-amber-200 bg-amber-50 p-4">
              <h3 className="font-bold text-amber-950">What needs correcting?</h3>
              {reportState === "sent" ? (
                <p className="mt-2 text-sm text-amber-900">Thanks—this is now in the Admin review queue.</p>
              ) : (
                <>
                  <select value={issueType} onChange={(event) => setIssueType(event.target.value)} className="mt-3 w-full rounded-lg border border-amber-300 bg-white px-3 py-2 text-sm">
                    {ISSUES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </select>
                  <textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={1000} rows={3} placeholder="Optional detail" className="mt-2 w-full rounded-lg border border-amber-300 bg-white px-3 py-2 text-sm" />
                  <button disabled={reportState === "sending"} className="mt-2 rounded-lg bg-amber-700 px-4 py-2 text-sm font-bold text-white disabled:opacity-50">
                    {reportState === "sending" ? "Sending…" : "Send report"}
                  </button>
                  {reportState === "error" && <span className="ml-2 text-xs text-red-700">Could not send. Try again.</span>}
                </>
              )}
            </form>
          )}
        </div>
      </aside>
    </div>
  );
}
