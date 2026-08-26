import { useState } from "react";
import { pickForMe, pickReason } from "./pickForMe.js";
import { replaceHashRouteFromClick } from "./hashNavigation.js";

/**
 * "Can't decide?" — suggests one restaurant with the evidence for why.
 *
 * Sits above the tabs so it reads as a shortcut past the filters, not as
 * another filter. Collapses back to a single button when dismissed.
 */
export default function PickForMe({ restaurants }) {
  const [pick, setPick] = useState(null);
  const [openOnly, setOpenOnly] = useState(false);
  const [empty, setEmpty] = useState(false);

  /**
   * Send the app to this restaurant on the map/list.
   *
   * replaceHashRouteFromClick returns false only for a modified click
   * (ctrl/cmd/middle) — those must fall through so the browser can open a new
   * tab. For a handled click it always returns true, but the underlying
   * replaceHashRoute skips its hashchange when the hash is ALREADY this
   * restaurant, so clicking the same pick twice (after scrolling away) would
   * do nothing. Fire the event ourselves in that case.
   */
  function focusPick(event, id) {
    const target = `#restaurants?restaurant=${id}`;
    const alreadyThere = window.location.hash === target;
    if (!replaceHashRouteFromClick(event, `restaurants?restaurant=${id}`)) return;
    if (alreadyThere) window.dispatchEvent(new Event("hashchange"));
  }

  function roll(nextOpenOnly = openOnly) {
    const next = pickForMe(restaurants, {
      openNow: nextOpenOnly,
      excludeId: pick?.id ?? null,
    });
    setEmpty(!next);
    if (next) setPick(next);
  }

  if (!restaurants || restaurants.length === 0) return null;

  return (
    <div className="mt-4 rounded-xl border border-stone-200 bg-white p-3 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => roll()}
          className="rounded-lg bg-emerald-700 px-3.5 py-2 text-sm font-bold text-white shadow-sm transition hover:bg-emerald-800"
        >
          {pick ? "↻ Pick another" : "✨ Pick somewhere for me"}
        </button>

        <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-semibold text-stone-500">
          <input
            type="checkbox"
            checked={openOnly}
            onChange={(event) => {
              const next = event.target.checked;
              setOpenOnly(next);
              if (pick) roll(next);
            }}
            className="h-3.5 w-3.5 accent-emerald-700"
          />
          Open now only
        </label>

        {pick && (
          <button
            type="button"
            onClick={() => {
              setPick(null);
              setEmpty(false);
            }}
            className="ml-auto text-xs font-semibold text-stone-400 transition hover:text-stone-600"
          >
            Dismiss
          </button>
        )}
      </div>

      {empty && !pick && (
        <p className="mt-2 text-xs text-stone-500">
          Nothing matches right now — try unchecking &ldquo;Open now&rdquo;.
        </p>
      )}

      {pick && (
        <div className="mt-3 border-t border-stone-100 pt-3">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <a
              href={`#restaurants?restaurant=${pick.id}`}
              onClick={(event) => focusPick(event, pick.id)}
              className="text-base font-extrabold text-stone-900 hover:text-emerald-700 hover:underline"
            >
              {pick.name}
            </a>
            {pick.vegan_score != null && (
              <span className="rounded-full bg-emerald-700 px-2 py-0.5 text-xs font-bold text-white">
                {pick.vegan_score.toFixed(1)}
              </span>
            )}
            {pick.open_now ? (
              <span className="text-xs font-bold text-emerald-700">Open now</span>
            ) : (
              <span className="text-xs font-bold text-rose-600">Closed</span>
            )}
          </div>
          <p className="mt-1 text-sm leading-relaxed text-stone-600">
            {pickReason(pick)}
          </p>
          {pick.address && (
            <p className="mt-1 text-xs text-stone-400">{pick.address}</p>
          )}
        </div>
      )}
    </div>
  );
}
