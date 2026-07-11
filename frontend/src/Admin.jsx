import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import DishModal from "./DishModal.jsx";
import RatingBadge from "./RatingBadge.jsx";
import {
  FreshnessBadge,
  OpenStatusBadge,
  isMenuStale,
  relativeDate,
} from "./RestaurantMeta.jsx";

// The pipeline dashboard (admin view). Talks only to our own backend
// (proxied /api/*), so no keys ever reach the browser. The consumer-facing
// view lives in Explore.jsx.

// Historical labels remain so older classification records still render;
// current classification is always performed by DeepSeek.
const PROVIDER_LABELS = {
  claude: "Claude subscription",
  codex: "Codex subscription",
  anthropic: "Anthropic API",
  deepseek: "DeepSeek",
};

// Non-food primary types a gap sweep matches but that shouldn't enter the
// pipeline (a pet bakery, a mall, a liquor store). Mirrors the backend
// venue_filter.EXCLUDED_PRIMARY_TYPES so the pre-add filter agrees with it.
const EXCLUDED_SWEEP_TYPES = new Set([
  "convenience_store",
  "gas_station",
  "grocery_store",
  "supermarket",
  "liquor_store",
  "pharmacy",
  "store",
  "shopping_mall",
  "pet_care",
]);

const isFoodVenue = (place) => !EXCLUDED_SWEEP_TYPES.has(place.primary_type);

// Backend caps a names-only add at 60 places per request; larger sets are
// split into sequential batches.
const ADD_BATCH_SIZE = 60;

// "resets_at" from the usage endpoints may be an ISO string or an epoch in
// seconds/milliseconds — render whatever arrives as a countdown.
function formatReset(resetsAt) {
  if (resetsAt == null) return null;
  let target;
  if (typeof resetsAt === "number") {
    target = new Date(resetsAt > 1e12 ? resetsAt : resetsAt * 1000);
  } else {
    target = new Date(resetsAt);
  }
  if (isNaN(target.getTime())) return null;
  const minutes = Math.max(0, Math.round((target - Date.now()) / 60000));
  if (minutes < 60) return `resets in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `resets in ${hours}h ${minutes % 60}m`;
  return `resets in ${Math.round(hours / 24)}d`;
}

function UsageBar({ window: w }) {
  const pct = Math.min(100, Math.max(0, w.used_pct));
  const tone =
    pct >= 80 ? "bg-red-500" : pct >= 50 ? "bg-amber-500" : "bg-emerald-500";
  const reset = formatReset(w.resets_at);
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-36 shrink-0 text-slate-500">{w.label}</span>
      <div className="h-2 min-w-24 flex-1 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full ${tone} transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="w-20 shrink-0 text-right font-semibold text-slate-700">
        {pct.toFixed(0)}% used
      </span>
      <span
        className="w-28 shrink-0 text-right text-slate-400"
        title={w.note || undefined}
      >
        {w.note ? "fresh window" : reset}
      </span>
    </div>
  );
}

const CHANGE_STYLES = {
  added: "bg-emerald-100 text-emerald-800",
  removed: "bg-rose-100 text-rose-700",
  price_changed: "bg-amber-100 text-amber-800",
  verdict_changed: "bg-violet-100 text-violet-800",
};

function changeDetail(change) {
  if (change.change_type === "price_changed") {
    return `${change.old_price || "—"} → ${change.new_price || "—"}`;
  }
  if (change.change_type === "verdict_changed") {
    return `${(change.old_verdict || "?").replaceAll("_", " ")} → ${(
      change.new_verdict || "?"
    ).replaceAll("_", " ")}`;
  }
  if (change.change_type === "added") return change.new_price || "";
  return change.old_price || "";
}

// "Why did this menu fail?" — restaurants whose last scrape attempt failed,
// each expandable to the scraper's per-URL trail: every URL it fetched, the
// stage (http/headless), the menu score, and the keep/reject decision. The
// evidence for deciding between a scraper fix, a manual URL, and the
// photo-fallback queue. Self-contained: fetches /api/scrape-failures.
function ScrapeFailuresPanel() {
  const [failures, setFailures] = useState(null);
  const [open, setOpen] = useState(false);
  const [expandedId, setExpandedId] = useState(null);
  const [retrying, setRetrying] = useState(null); // restaurant id

  const load = () =>
    fetch("/api/scrape-failures")
      .then((res) => (res.ok ? res.json() : { failures: [] }))
      .then((data) => setFailures(data.failures || []))
      .catch(() => setFailures([]));

  useEffect(() => {
    load();
  }, []);

  async function retry(failure) {
    setRetrying(failure.id);
    try {
      await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ restaurant_id: failure.id }),
      });
      await load();
    } finally {
      setRetrying(null);
    }
  }

  if (!failures || failures.length === 0) return null;

  const stamp = (value) =>
    value
      ? new Date(value).toLocaleString([], {
          month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
        })
      : "never";

  return (
    <section className="mb-6 rounded-xl border border-rose-200 bg-rose-50 p-4">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-left"
      >
        <h2 className="font-bold text-rose-950">
          Scrape failures
          <span className="ml-2 text-xs font-medium text-rose-700">
            why each menu failed — per-URL evidence
          </span>
        </h2>
        <span className="rounded-full bg-rose-200 px-2 py-0.5 text-xs font-bold text-rose-900">
          {failures.length} {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="mt-3 space-y-2">
          {failures.map((f) => (
            <div key={f.id} className="rounded-lg bg-white p-3 text-sm shadow-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <span className="font-bold text-slate-900">{f.name}</span>
                  <span className="ml-2 text-xs font-semibold text-rose-700">
                    {f.consecutive_failures} failed attempt
                    {f.consecutive_failures === 1 ? "" : "s"}
                  </span>
                  <span className="ml-2 text-xs text-slate-400">
                    last tried {stamp(f.last_attempt_at)}
                    {f.last_success_at &&
                      ` · last success ${stamp(f.last_success_at)}`}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-3 text-xs font-bold">
                  {f.website_url && (
                    <a
                      href={f.website_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-slate-600 hover:text-emerald-700"
                    >
                      Website ↗
                    </a>
                  )}
                  {(f.diagnostics?.length ?? 0) > 0 && (
                    <button
                      onClick={() =>
                        setExpandedId(expandedId === f.id ? null : f.id)
                      }
                      className="text-slate-600 hover:text-slate-900"
                    >
                      {expandedId === f.id
                        ? "Hide trail ▴"
                        : `URL trail (${f.diagnostics.length}) ▾`}
                    </button>
                  )}
                  <button
                    onClick={() => retry(f)}
                    disabled={retrying !== null}
                    className="rounded-lg border border-emerald-300 px-2.5 py-1 text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:text-slate-400"
                  >
                    {retrying === f.id ? "Scraping…" : "Retry"}
                  </button>
                </div>
              </div>
              <div className="mt-1 text-xs text-rose-800">{f.last_error}</div>
              {expandedId === f.id && (
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="text-[10px] font-bold uppercase tracking-wide text-slate-400">
                        <th className="py-1 pr-3">Stage</th>
                        <th className="py-1 pr-3">Score</th>
                        <th className="py-1 pr-3">Prices</th>
                        <th className="py-1 pr-3">Food words</th>
                        <th className="py-1 pr-3">Decision</th>
                        <th className="py-1">URL</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {f.diagnostics.map((d, i) => (
                        <tr key={i} className="text-slate-600">
                          <td className="py-1 pr-3">{d.stage}</td>
                          <td className={`py-1 pr-3 font-semibold ${
                            (d.score ?? 0) >= 0.45 ? "text-emerald-700" : "text-slate-500"
                          }`}>
                            {d.score?.toFixed(2) ?? "—"}
                          </td>
                          <td className="py-1 pr-3">{d.prices ?? "—"}</td>
                          <td className="py-1 pr-3">{d.food_words ?? "—"}</td>
                          <td className={`py-1 pr-3 font-semibold ${
                            d.decision === "keep" ? "text-emerald-700" : "text-rose-600"
                          }`}>
                            {d.decision}
                          </td>
                          <td className="max-w-[360px] truncate py-1">
                            <a
                              href={d.url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-sky-700 hover:underline"
                              title={d.url}
                            >
                              {d.url}
                            </a>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// Menu history per restaurant: distinct raw-menu versions (immutable, one
// per actual content change) + the dish-change log (added/removed/price/
// verdict transitions recorded at each reclassification).
function HistoryModal({ restaurant, onClose }) {
  const [versions, setVersions] = useState(null);
  const [changes, setChanges] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [contents, setContents] = useState(null); // version id -> raw text

  useEffect(() => {
    if (!restaurant) return;
    setVersions(null);
    setChanges(null);
    setExpandedId(null);
    setContents(null);
    fetch(`/api/restaurants/${restaurant.id}/menu-versions`)
      .then((res) => (res.ok ? res.json() : { versions: [] }))
      .then((data) => setVersions(data.versions || []))
      .catch(() => setVersions([]));
    fetch(`/api/restaurants/${restaurant.id}/dish-changes`)
      .then((res) => (res.ok ? res.json() : { changes: [] }))
      .then((data) => setChanges(data.changes || []))
      .catch(() => setChanges([]));
  }, [restaurant]);

  async function toggleVersion(id) {
    if (expandedId === id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(id);
    if (contents === null) {
      try {
        const res = await fetch(
          `/api/restaurants/${restaurant.id}/menu-versions?full=1`
        );
        const data = await res.json();
        const map = {};
        for (const v of data.versions || []) map[v.id] = v.content;
        setContents(map);
      } catch {
        setContents({});
      }
    }
  }

  if (!restaurant) return null;

  const changeGroups = [];
  if (changes) {
    const byTime = new Map();
    for (const change of changes) {
      if (!byTime.has(change.observed_at)) {
        const group = { at: change.observed_at, items: [] };
        byTime.set(change.observed_at, group);
        changeGroups.push(group);
      }
      byTime.get(change.observed_at).items.push(change);
    }
  }
  const stamp = (value) =>
    new Date(value).toLocaleString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });

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
          <h2 className="font-semibold text-slate-900">
            Menu history — {restaurant.name}
          </h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            ✕
          </button>
        </div>
        <div className="space-y-5 overflow-y-auto p-4">
          <section>
            <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-slate-400">
              Menu versions
              <span className="ml-1.5 font-medium normal-case">
                — one row per actual content change; identical recrawls add nothing
              </span>
            </h3>
            {versions === null ? (
              <div className="text-sm text-slate-400">Loading…</div>
            ) : versions.length === 0 ? (
              <div className="text-sm text-slate-400">
                No versions captured yet — recording starts with the next crawl
                of this restaurant.
              </div>
            ) : (
              <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
                {versions.map((v, i) => (
                  <li key={v.id} className="px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="font-medium text-slate-800">
                        {stamp(v.fetched_at)}
                      </span>
                      {i === 0 && (
                        <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-bold uppercase text-emerald-700">
                          current
                        </span>
                      )}
                      <span className="text-xs text-slate-400">
                        {v.char_count?.toLocaleString()} chars
                        {v.menu_score != null && ` · score ${v.menu_score.toFixed(2)}`}
                        {" · "}
                        <span className="font-mono">{v.content_hash?.slice(0, 10)}</span>
                      </span>
                      <button
                        onClick={() => toggleVersion(v.id)}
                        className="ml-auto text-xs font-semibold text-emerald-700 hover:underline"
                      >
                        {expandedId === v.id ? "hide text" : "view text"}
                      </button>
                    </div>
                    {expandedId === v.id && (
                      <pre className="mt-2 max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3 font-mono text-xs text-slate-700">
                        {contents === null
                          ? "Loading…"
                          : contents[v.id] || "(content unavailable)"}
                      </pre>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-slate-400">
              Dish changes
              <span className="ml-1.5 font-medium normal-case">
                — recorded at each reclassification of a changed menu
              </span>
            </h3>
            {changes === null ? (
              <div className="text-sm text-slate-400">Loading…</div>
            ) : changeGroups.length === 0 ? (
              <div className="text-sm text-slate-400">
                No dish changes recorded yet — they accumulate as changed menus
                are reclassified.
              </div>
            ) : (
              <div className="space-y-3">
                {changeGroups.map((group) => (
                  <div key={group.at} className="rounded-lg border border-slate-200">
                    <div className="border-b border-slate-100 bg-slate-50 px-3 py-1.5 text-xs font-semibold text-slate-500">
                      {stamp(group.at)} · {group.items.length} change
                      {group.items.length === 1 ? "" : "s"}
                    </div>
                    <ul className="divide-y divide-slate-50">
                      {group.items.map((change, idx) => (
                        <li
                          key={`${change.dish_name}-${idx}`}
                          className="flex flex-wrap items-baseline gap-2 px-3 py-1.5 text-sm"
                        >
                          <span
                            className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${
                              CHANGE_STYLES[change.change_type] ||
                              "bg-slate-100 text-slate-600"
                            }`}
                          >
                            {change.change_type.replaceAll("_", " ")}
                          </span>
                          <span className="font-medium text-slate-800">
                            {change.dish_name}
                          </span>
                          <span className="text-xs text-slate-400">
                            {changeDetail(change)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}


// Area prospecting: search any Google Places query ("restaurants on Mills
// Ave Orlando"), see everything on a map + list — names only — and pull
// selected places into the pipeline. Scraping/classification run later from
// the Active table's bulk tools, so pulling 40 names in stays instant.
function ProspectPanel({ onAdded, config, defaultProvider = "deepseek" }) {
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [sweepCenter, setSweepCenter] = useState({
    lat: config?.lat ?? 28.5383,
    lng: config?.lng ?? -81.3792,
  });
  const [radiusMiles, setRadiusMiles] = useState(5);
  const [sweepEstimate, setSweepEstimate] = useState(null);
  const [estimating, setEstimating] = useState(false);
  const [sweeping, setSweeping] = useState(false);
  const [onlyNew, setOnlyNew] = useState(false);
  const [places, setPlaces] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);
  // Full-pipeline run state: add -> background scrape job -> background
  // classify job, all scoped to the newly added ids.
  const [pipeline, setPipeline] = useState(null);
  const [pipelineProvider, setPipelineProvider] = useState(defaultProvider);
  const mapEl = useRef(null);
  const mapRef = useRef(null);

  async function runSearch(event) {
    event?.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/prospect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Search failed (${res.status})`);
      setPlaces(data.places);
      setSelected(new Set());
    } catch (e) {
      setError(e.message);
    } finally {
      setSearching(false);
    }
  }

  const radiusMeters = radiusMiles * 1609.344;

  function moveSweepCenter(lat, lng) {
    setSweepCenter({ lat, lng });
    setSweepEstimate(null);
  }

  async function estimateSweep(event) {
    event?.preventDefault();
    if (estimating || sweeping) return;
    setEstimating(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/prospect/radius/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lat: sweepCenter.lat,
          lng: sweepCenter.lng,
          radius_meters: radiusMeters,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Estimate failed (${res.status})`);
      setSweepEstimate(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setEstimating(false);
    }
  }

  async function runSweep() {
    if (!sweepEstimate || sweeping) return;
    setSweeping(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/prospect/radius", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lat: sweepCenter.lat,
          lng: sweepCenter.lng,
          radius_meters: radiusMeters,
          confirmed_call_budget: sweepEstimate.call_budget,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Sweep failed (${res.status})`);
      setPlaces(data.places);
      setSelected(new Set());
      setOnlyNew(data.new_count > 0);
      setNotice(
        `Radius sweep found ${data.count} food venues in ${data.calls_run} ` +
          `Google searches — ${data.new_count} not in the pipeline yet.` +
          (data.saturated_calls
            ? ` ${data.saturated_calls} dense cells hit Google's 20-result cap.`
            : "") +
          (data.errors?.length ? ` ${data.errors.length} searches failed.` : "")
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setSweeping(false);
    }
  }

  function toggle(place) {
    if (place.already_added_id) return;
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(place.place_id)) next.delete(place.place_id);
      else next.add(place.place_id);
      return next;
    });
  }

  const newPlaces = (places || []).filter((p) => !p.already_added_id);
  // What the list and map actually show — optionally just the gaps.
  const displayed = useMemo(
    () => (onlyNew ? (places || []).filter((p) => !p.already_added_id) : places),
    [places, onlyNew]
  );

  // New (not-yet-added) places split into real food venues vs excluded types.
  const { keepers, droppedByType } = useMemo(() => {
    const kept = [];
    const dropped = new Map();
    for (const p of newPlaces) {
      if (isFoodVenue(p)) {
        kept.push(p);
      } else {
        const t = p.primary_type || "unknown";
        dropped.set(t, (dropped.get(t) || 0) + 1);
      }
    }
    return {
      keepers: kept,
      droppedByType: [...dropped.entries()].sort((a, b) => b[1] - a[1]),
    };
  }, [newPlaces]);
  const droppedCount = droppedByType.reduce((sum, [, n]) => sum + n, 0);

  // Add every food-venue keeper, names-only, in <=60 batches (the backend
  // per-request cap). Enrichment only — scrape/classify run later as jobs.
  async function addAllKeepers() {
    if (keepers.length === 0 || adding) return;
    setAdding(true);
    setError(null);
    setNotice(null);
    try {
      const addedIds = new Map();
      let added = 0;
      for (let i = 0; i < keepers.length; i += ADD_BATCH_SIZE) {
        const batch = keepers.slice(i, i + ADD_BATCH_SIZE);
        setNotice(
          `Adding ${added + 1}–${Math.min(
            added + batch.length,
            keepers.length
          )} of ${keepers.length}…`
        );
        const res = await fetch("/api/restaurants/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ places: batch, ingest: false, classify: false }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `Add failed (${res.status})`);
        batch.forEach((p, j) => addedIds.set(p.place_id, data.added?.[j]?.id ?? true));
        added += batch.length;
      }
      setPlaces((current) =>
        (current || []).map((p) =>
          addedIds.has(p.place_id)
            ? { ...p, already_added_id: addedIds.get(p.place_id) }
            : p
        )
      );
      setSelected(new Set());
      setNotice(
        `Added ${added} food venue(s)${
          droppedCount ? ` (skipped ${droppedCount} non-food)` : ""
        } — scrape & classify them from the Active table when ready.`
      );
      onAdded?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setAdding(false);
    }
  }

  async function addSelected() {
    const chosen = (places || []).filter((p) => selected.has(p.place_id));
    if (chosen.length === 0) return;
    setAdding(true);
    setError(null);
    try {
      const byPlaceId = new Map();
      let added = 0;
      for (let i = 0; i < chosen.length; i += ADD_BATCH_SIZE) {
        const batch = chosen.slice(i, i + ADD_BATCH_SIZE);
        setNotice(
          `Adding ${added + 1}–${Math.min(added + batch.length, chosen.length)} ` +
            `of ${chosen.length}…`
        );
        const res = await fetch("/api/restaurants/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ places: batch, ingest: false, classify: false }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `Add failed (${res.status})`);
        batch.forEach((p, index) =>
          byPlaceId.set(p.place_id, data.added?.[index]?.id ?? true)
        );
        added += batch.length;
      }
      setPlaces((current) =>
        (current || []).map((p) =>
          byPlaceId.has(p.place_id)
            ? { ...p, already_added_id: byPlaceId.get(p.place_id) }
            : p
        )
      );
      setSelected(new Set());
      setNotice(
        `Added ${chosen.length} restaurant(s) — scrape & classify them from ` +
          `the Active table when ready.`
      );
      onAdded?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setAdding(false);
    }
  }

  // Add the selected places (names only), then chain the existing background
  // jobs against exactly those ids: scrape, then classify. Progress is shown
  // inline by polling the job status endpoints the dashboards already use.
  async function addAndRunAll(explicitPlaces = null) {
    const chosen = Array.isArray(explicitPlaces)
      ? explicitPlaces
      : (places || []).filter((p) => selected.has(p.place_id));
    if (chosen.length === 0 || pipeline) return;
    if (chosen.length > 1000) {
      setError("One-go runs support up to 1,000 places. Narrow the selection.");
      return;
    }
    setError(null);
    setNotice(null);

    const poll = async (statusUrl, label) => {
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const res = await fetch(statusUrl);
        if (!res.ok) throw new Error(`${label} status check failed`);
        const state = await res.json();
        setPipeline({
          stage: label,
          done: state.done ?? 0,
          total: state.total,
          current: state.current,
          cost: state.cost,
        });
        if (!state.running) {
          if (state.error) throw new Error(`${label} failed: ${state.error}`);
          return state;
        }
      }
    };

    try {
      setPipeline({ stage: "Adding", done: 0, total: chosen.length });
      const ids = [];
      const byPlaceId = new Map();
      for (let i = 0; i < chosen.length; i += ADD_BATCH_SIZE) {
        const batch = chosen.slice(i, i + ADD_BATCH_SIZE);
        setPipeline({
          stage: "Adding",
          done: i,
          total: chosen.length,
          current: `${i + 1}–${Math.min(i + batch.length, chosen.length)}`,
        });
        const addRes = await fetch("/api/restaurants/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ places: batch, ingest: false, classify: false }),
        });
        const addData = await addRes.json();
        if (!addRes.ok) throw new Error(addData.error || "Add failed");
        (addData.added || []).forEach((entry) => {
          if (entry.id) ids.push(entry.id);
        });
        batch.forEach((p, index) =>
          byPlaceId.set(p.place_id, addData.added?.[index]?.id ?? true)
        );
      }
      setPlaces((current) =>
        (current || []).map((p) =>
          byPlaceId.has(p.place_id)
            ? { ...p, already_added_id: byPlaceId.get(p.place_id) }
            : p
        )
      );
      setSelected(new Set());
      onAdded?.();
      if (ids.length === 0) throw new Error("Nothing was added.");

      // then_classify makes the handoff SERVER-side: the classify job
      // launches from the scrape worker, so navigating away (or closing
      // the browser) can no longer strand the run half-done.
      setPipeline({ stage: "Scraping", done: 0, total: ids.length });
      const ingestRes = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          restaurant_ids: ids,
          then_classify: { provider: pipelineProvider, parallel: 3 },
        }),
      });
      if (!ingestRes.ok) {
        const data = await ingestRes.json();
        throw new Error(data.error || "Scrape job could not start");
      }
      const ingestState = await poll("/api/ingest/status", "Scraping");
      if (
        ingestState.chained_classify &&
        ingestState.chained_classify !== "started"
      ) {
        throw new Error(
          `Classification could not start: ${ingestState.chained_classify}`
        );
      }

      setPipeline({ stage: "Classifying", done: 0, total: null });
      const classifyState = await poll("/api/classify/status", "Classifying");

      const cost = classifyState.summary?.cost;
      setNotice(
        `One-go run finished: ${ids.length} added, ` +
          `${ingestState.succeeded ?? "?"} menus scraped, ` +
          `${classifyState.summary?.dishes ?? "?"} dishes classified` +
          (cost ? ` ($${cost.toFixed(3)})` : "") +
          "."
      );
      onAdded?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setPipeline(null);
    }
  }

  // (Re)draw the map whenever the shown results or selection change —
  // trivial at a few hundred pins, and far simpler than incremental sync.
  useEffect(() => {
    if (!mapEl.current) return;
    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
    }
    const located = (displayed || []).filter(
      (p) => p.lat != null && p.lng != null
    );
    const center = [sweepCenter.lat, sweepCenter.lng];
    const map = L.map(mapEl.current, { scrollWheelZoom: true });
    // Leaflet circles calculate projected bounds through the map. Establish
    // an initial view first, and retain the map immediately so React Strict
    // Mode can always clean it up if anything later in setup throws.
    mapRef.current = map;
    map.setView(center, 11);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors",
    }).addTo(map);
    const sweepCircle = L.circle(center, {
      radius: radiusMeters,
      color: "#0284c7",
      weight: 2,
      opacity: 0.8,
      fillColor: "#38bdf8",
      fillOpacity: 0.08,
    }).addTo(map);
    const centerMarker = L.marker(center, { draggable: true }).addTo(map);
    centerMarker.bindTooltip("Sweep center — drag or click the map to move", {
      direction: "top",
    });
    centerMarker.on("dragend", (event) => {
      const point = event.target.getLatLng();
      moveSweepCenter(point.lat, point.lng);
    });
    centerMarker.on("click", (event) => {
      if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
    });
    map.on("click", (event) => moveSweepCenter(event.latlng.lat, event.latlng.lng));

    const bounds = sweepCircle.getBounds();
    for (const p of located) {
      bounds.extend([p.lat, p.lng]);
      const color = p.already_added_id
        ? "#a8a29e"
        : selected.has(p.place_id)
          ? "#047857"
          : "#ffffff";
      const marker = L.marker([p.lat, p.lng], {
        icon: L.divIcon({
          className: "",
          html: `<div style="background:${color};border:2px solid ${
            p.already_added_id ? "#a8a29e" : "#047857"
          };border-radius:9999px;width:14px;height:14px;box-shadow:0 1px 3px rgba(0,0,0,.4)"></div>`,
          iconSize: [14, 14],
          iconAnchor: [7, 7],
        }),
      }).addTo(map);
      marker.bindTooltip(
        `${p.name}${p.already_added_id ? " (already added)" : ""}`,
        { direction: "top", offset: [0, -6] }
      );
      marker.on("click", (event) => {
        if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
        toggle(p);
      });
    }
    map.fitBounds(bounds, { padding: [30, 30], maxZoom: 14 });
    return () => {
      if (mapRef.current === map) {
        map.remove();
        mapRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [displayed, selected, sweepCenter, radiusMeters]);

  return (
    <div className="space-y-3">
      <form onSubmit={runSearch} className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={'Try "restaurants on Mills Ave Orlando" or "restaurants in Winter Park"'}
          className="w-full max-w-lg rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
        />
        <button
          type="submit"
          disabled={searching || !query.trim()}
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {searching ? "Searching…" : "Search area"}
        </button>
        {places && (
          <span className="text-sm text-slate-500">
            {places.length} found · {newPlaces.length} new
          </span>
        )}
      </form>

      <form
        onSubmit={estimateSweep}
        className="rounded-xl border border-sky-200 bg-sky-50 p-3"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-bold text-sky-950">Radius sweep</span>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            Radius
            <input
              type="number"
              min="0.25"
              max="31"
              step="0.25"
              value={radiusMiles}
              onChange={(event) => {
                setRadiusMiles(Number(event.target.value));
                setSweepEstimate(null);
              }}
              className="w-20 rounded-lg border border-sky-300 bg-white px-2 py-1.5 text-sm outline-none focus:border-sky-500"
            />
            miles
          </label>
          <span className="text-xs tabular-nums text-slate-500">
            {sweepCenter.lat.toFixed(5)}, {sweepCenter.lng.toFixed(5)}
          </span>
          <button
            type="submit"
            disabled={estimating || sweeping || radiusMiles < 0.25}
            className="rounded-lg border border-sky-400 bg-white px-3 py-1.5 text-sm font-semibold text-sky-800 hover:bg-sky-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {estimating ? "Estimating…" : "Estimate sweep"}
          </button>
        </div>
        <div className="mt-1 text-xs text-sky-800">
          Click the map or drag its marker, choose a radius, then estimate and
          confirm. Searches restaurants plus breweries, cafes, bakeries,
          dessert and ice-cream shops, and other food venues.
        </div>
        {sweepEstimate && (
          <div className="mt-3 flex flex-wrap items-center gap-3 rounded-lg border border-sky-200 bg-white px-3 py-2 text-xs text-slate-700">
            <span>
              <strong>{sweepEstimate.cells}</strong> overlapping cells ×{" "}
              <strong>{sweepEstimate.type_groups}</strong> food groups
            </span>
            <span>
              <strong>{sweepEstimate.base_calls}</strong> Google calls
            </span>
            <span>
              list-price estimate <strong>~${sweepEstimate.estimated_list_cost.toFixed(2)}</strong>
            </span>
            {!sweepEstimate.within_call_limit ? (
              <span className="font-semibold text-red-700">
                Too large for the {sweepEstimate.call_budget}-call safety limit.
              </span>
            ) : (
              <button
                type="button"
                onClick={runSweep}
                disabled={sweeping}
                className="ml-auto rounded-lg bg-sky-700 px-3 py-1.5 font-bold text-white hover:bg-sky-800 disabled:bg-slate-300"
              >
                {sweeping
                  ? `Sweeping… (${sweepEstimate.base_calls} calls)`
                  : `Confirm & run ${sweepEstimate.base_calls} calls`}
              </button>
            )}
          </div>
        )}
      </form>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {notice}
        </div>
      )}

      {(!places || places.length === 0) && (
        <div
          ref={mapEl}
          className="z-0 h-[420px] overflow-hidden rounded-xl border border-slate-200"
        />
      )}

      {places && places.length > 0 && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() =>
                setSelected(new Set(newPlaces.map((p) => p.place_id)))
              }
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50"
            >
              Select all new ({newPlaces.length})
            </button>
            <button
              onClick={() => setSelected(new Set())}
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50"
            >
              Clear
            </button>
            <label className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50">
              <input
                type="checkbox"
                checked={onlyNew}
                onChange={(e) => setOnlyNew(e.target.checked)}
              />
              Only new ({newPlaces.length})
            </label>
            <button
              onClick={addSelected}
              disabled={adding || Boolean(pipeline) || selected.size === 0}
              className="rounded-lg bg-emerald-600 px-4 py-1.5 text-xs font-bold text-white shadow-sm hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {adding
                ? "Adding…"
                : `Add ${selected.size} to pipeline (names only)`}
            </button>
            {keepers.length > 0 && (
              <>
                <button
                  onClick={addAllKeepers}
                  disabled={adding || Boolean(pipeline)}
                  className="rounded-lg border border-emerald-300 bg-white px-4 py-1.5 text-xs font-bold text-emerald-800 shadow-sm hover:bg-emerald-50 disabled:cursor-not-allowed disabled:bg-slate-100"
                  title={
                    `Adds all ${keepers.length} food venues in batches, but does ` +
                    `not scrape or classify them.` +
                    (droppedCount ? ` Skips ${droppedCount} non-food venue(s).` : "")
                  }
                >
                  {adding ? "Adding…" : `Add all only (${keepers.length})`}
                </button>
                <button
                  onClick={() => addAndRunAll(keepers)}
                  disabled={adding || Boolean(pipeline)}
                  className="rounded-lg bg-violet-700 px-4 py-1.5 text-xs font-bold text-white shadow-sm hover:bg-violet-800 disabled:cursor-not-allowed disabled:bg-slate-300"
                  title={`Add all ${keepers.length} food venues, scrape their menus, then classify every successful menu with DeepSeek`}
                >
                  {pipeline
                    ? `${pipeline.stage}…`
                    : `Add all + scrape + classify (${keepers.length})`}
                </button>
              </>
            )}
            <span className="mx-1 text-xs text-slate-300">|</span>
            <select
              value={pipelineProvider}
              onChange={(e) => setPipelineProvider(e.target.value)}
              disabled={Boolean(pipeline)}
              className="rounded-lg border border-violet-300 bg-white px-2 py-1.5 text-xs font-semibold text-violet-800 shadow-sm disabled:text-slate-400"
              aria-label="Classification provider for the one-go run"
            >
              <option
                value="deepseek"
                disabled={!config?.classifier?.providers?.deepseek?.available}
              >
                DeepSeek
              </option>
            </select>
            <button
              onClick={() => addAndRunAll()}
              disabled={Boolean(pipeline) || adding || selected.size === 0}
              className="rounded-lg bg-violet-600 px-4 py-1.5 text-xs font-bold text-white shadow-sm hover:bg-violet-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              title="Add the selected places, then scrape their menus and classify the dishes as chained background jobs"
            >
              {pipeline
                ? `${pipeline.stage}…`
                : `Add + scrape + classify (${selected.size})`}
            </button>
          </div>

          {droppedCount > 0 && (
            <div className="text-xs text-slate-500">
              “Add all food venues” skips {droppedCount} non-food result(s):{" "}
              {droppedByType
                .map(([type, n]) => `${n} ${type.replace(/_/g, " ")}`)
                .join(", ")}
              .
            </div>
          )}

          {pipeline && (
            <div className="flex items-center gap-2 rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-xs font-semibold text-violet-900">
              <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-violet-600 border-t-transparent" />
              {pipeline.stage}
              {pipeline.total != null && ` ${pipeline.done}/${pipeline.total}`}
              {pipeline.current && ` — ${pipeline.current}`}
              {pipeline.cost > 0 && ` · $${pipeline.cost.toFixed(3)}`}
            </div>
          )}

          <div className="grid gap-3 lg:grid-cols-2">
            <div
              ref={mapEl}
              className="z-0 h-[420px] overflow-hidden rounded-xl border border-slate-200"
            />
            <div className="max-h-[420px] overflow-y-auto rounded-xl border border-slate-200 bg-white">
              {displayed.length === 0 && (
                <div className="p-8 text-center text-sm text-slate-400">
                  Everything found here is already in the pipeline. 🎉
                </div>
              )}
              <ul className="divide-y divide-slate-100">
                {displayed.map((p) => (
                  <li key={p.place_id}>
                    <label
                      className={`flex cursor-pointer items-start gap-2 px-3 py-2 text-sm ${
                        p.already_added_id ? "opacity-50" : "hover:bg-slate-50"
                      }`}
                    >
                      <input
                        type="checkbox"
                        className="mt-1"
                        disabled={Boolean(p.already_added_id)}
                        checked={selected.has(p.place_id)}
                        onChange={() => toggle(p)}
                      />
                      <span>
                        <span className="font-medium text-slate-900">
                          {p.name}
                        </span>
                        {p.already_added_id && (
                          <span className="ml-1.5 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-bold uppercase text-sky-800">
                            already added
                          </span>
                        )}
                        <span className="block text-xs text-slate-500">
                          {p.address}
                        </span>
                        {p.distance_meters != null && (
                          <span className="block text-[11px] text-sky-700">
                            {(p.distance_meters / 1609.344).toFixed(1)} mi from
                            marker
                            {p.primary_type
                              ? ` · ${p.primary_type.replaceAll("_", " ")}`
                              : ""}
                          </span>
                        )}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </>
      )}

      {places && places.length === 0 && (
        <div className="rounded-xl border border-slate-200 bg-white p-8 text-center text-slate-400">
          No places found for that search.
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, hint }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-2xl font-semibold text-slate-900">{value}</div>
      <div className="text-sm font-medium text-slate-600">{label}</div>
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </div>
  );
}

function classificationAgeGroup(value) {
  if (!value) return "Never classified";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "Never classified";
  const ageDays = Math.max(0, (Date.now() - timestamp) / 86_400_000);
  if (ageDays <= 7) return "Classified in the past 7 days";
  if (ageDays <= 30) return "Classified 8–30 days ago";
  return "Classified over 30 days ago";
}

function classificationDate(value) {
  if (!value) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function menuWorkload(value) {
  const chars = Number(value) || 0;
  const formatted =
    chars >= 10_000
      ? `${Math.round(chars / 1000)}k chars`
      : chars >= 1000
        ? `${(chars / 1000).toFixed(1)}k chars`
        : `${chars.toLocaleString()} chars`;
  if (chars <= 0) {
    return { formatted, label: "No menu", runtime: "—", style: "bg-slate-100 text-slate-500" };
  }
  if (chars <= 5000) {
    return { formatted, label: "Small", runtime: "<2 min", style: "bg-emerald-100 text-emerald-800" };
  }
  if (chars <= 15_000) {
    return { formatted, label: "Medium", runtime: "2–5 min", style: "bg-sky-100 text-sky-800" };
  }
  if (chars <= 30_000) {
    return { formatted, label: "Large", runtime: "4–8 min", style: "bg-amber-100 text-amber-800" };
  }
  return {
    formatted,
    label: chars > 50_000 ? "Very large · 50k cap" : "Very large",
    runtime: "7–12+ min",
    style: "bg-rose-100 text-rose-800",
  };
}

// Live progress for a background pipeline job (bulk scrape / bulk classify).
// `job` is the polled /api/<job>/status payload; classify jobs also carry a
// running API-cost total and per-restaurant costs.
function JobProgressPanel({ job, title, onStop }) {
  if (!job?.running) return null;
  return (
    <section className="mb-6 rounded-xl border border-emerald-200 bg-white p-4 shadow-sm">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold text-slate-900">
            {title}…{" "}
            {job.total != null ? `${job.done} of ${job.total}` : "preparing"}
          </span>
          {job.provider && (
            <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] font-bold text-violet-800">
              {PROVIDER_LABELS[job.provider] || job.provider}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-slate-500">
            <span className="font-semibold text-emerald-700">{job.succeeded} ok</span>
            {" · "}
            <span className={job.failed > 0 ? "font-semibold text-amber-700" : ""}>
              {job.failed} failed
            </span>
            {job.billing === "api" && job.cost != null && (
              <>
                {" · "}
                <span className="font-semibold text-slate-700">
                  ~${job.cost.toFixed(2)} so far
                </span>
              </>
            )}
          </span>
          {onStop && (
            <button
              type="button"
              onClick={onStop}
              disabled={job.cancel_requested}
              className="rounded-lg border border-rose-200 px-3 py-1 text-xs font-bold text-rose-700 hover:bg-rose-50 disabled:cursor-wait disabled:border-slate-200 disabled:text-slate-400"
            >
              {job.cancel_requested ? "Stopping…" : "Stop"}
            </button>
          )}
        </div>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full rounded-full bg-emerald-500 transition-all duration-500"
          style={{
            width: job.total
              ? `${Math.round((job.done / job.total) * 100)}%`
              : "4%",
          }}
        />
      </div>
      {job.current && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
          now: <span className="font-medium text-slate-700">{job.current}</span>
          <span className="text-slate-400">
            · waiting for {PROVIDER_LABELS[job.provider] || "the provider"} response
          </span>
        </div>
      )}
      {job.recent?.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs">
          {job.recent.slice(0, 5).map((r) => (
            <li key={r.name} className="flex items-baseline gap-2">
              {r.ok ? (
                <>
                  <span className="text-emerald-600">✓</span>
                  <span className="font-medium text-slate-700">{r.name}</span>
                  <span className="text-slate-400">
                    {r.dishes != null
                      ? `${r.dishes} dishes` +
                        (r.veganish != null ? `, ${r.veganish} veganish` : "") +
                        (r.cost != null ? ` · ~$${r.cost.toFixed(2)}` : "") +
                        (r.provider ? ` · ${r.provider}` : "")
                      : `${r.pages} page${r.pages === 1 ? "" : "s"}, ${r.chars} chars, score ${r.score?.toFixed(2)}`}
                  </span>
                </>
              ) : (
                <>
                  <span className="text-amber-600">✗</span>
                  <span className="font-medium text-slate-700">{r.name}</span>
                  <span className="truncate text-slate-400" title={r.error}>
                    {r.error}
                  </span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// Pipeline stage of one restaurant, in funnel order. Mutually exclusive —
// every active consumer venue lands in exactly one bucket, so the coverage
// counts always sum to the area total.
function pipelineStage(restaurant) {
  if ((restaurant.dish_count || 0) > 0) return "classified";
  if (restaurant.has_menu_text) return "ready";
  if (!restaurant.website_url) return "no_website";
  if ((restaurant.crawl_failures || 0) > 0) return "scrape_failing";
  return "needs_scrape";
}

// [stage key, column label, operationalFilter value for drill-down, cell tint]
const COVERAGE_STAGES = [
  ["classified", "Classified", "classified", "text-emerald-700"],
  ["ready", "Ready to classify", "ready_to_classify", "text-violet-700"],
  ["needs_scrape", "Needs scrape", "needs_menu", "text-amber-700"],
  ["scrape_failing", "Scrape failing", "scrape_failing", "text-rose-700"],
  ["no_website", "No website", "no_website", "text-slate-500"],
];

// Metro/area pipeline funnel — the "classify every restaurant in Orlando"
// progress view. Cities roll up into their metro (Maitland is Orlando);
// every count drills into the restaurant table below.
function AreaCoverage({ restaurants, onDrill }) {
  const [open, setOpen] = useState(true);

  const emptyRow = (label) => ({
    label,
    total: 0,
    classified: 0,
    ready: 0,
    needs_scrape: 0,
    scrape_failing: 0,
    no_website: 0,
  });

  const metros = useMemo(() => {
    const byMetro = new Map();
    for (const restaurant of restaurants) {
      // The funnel tracks real work: active, consumer-facing venues only.
      if (restaurant.archived || !restaurant.is_consumer_venue) continue;
      const area = restaurant.area || "Unknown";
      const metro = restaurant.metro || area;
      if (!byMetro.has(metro)) {
        byMetro.set(metro, { row: emptyRow(metro), cities: new Map() });
      }
      const entry = byMetro.get(metro);
      if (!entry.cities.has(area)) entry.cities.set(area, emptyRow(area));
      const stage = pipelineStage(restaurant);
      for (const row of [entry.row, entry.cities.get(area)]) {
        row.total += 1;
        row[stage] += 1;
      }
    }
    return [...byMetro.values()]
      .map((entry) => ({
        row: entry.row,
        cities: [...entry.cities.values()].sort(
          (a, b) => b.total - a.total || a.label.localeCompare(b.label)
        ),
      }))
      .sort(
        (a, b) =>
          b.row.total - a.row.total || a.row.label.localeCompare(b.row.label)
      );
  }, [restaurants]);

  const totals = useMemo(() => {
    const sum = emptyRow("Everywhere");
    for (const { row } of metros) {
      for (const key of Object.keys(sum)) {
        if (key !== "label") sum[key] += row[key];
      }
    }
    return sum;
  }, [metros]);

  if (metros.length === 0) return null;

  const pct = (row) =>
    row.total > 0 ? Math.round((row.classified / row.total) * 100) : 0;

  const countCell = (row, drillValue, stage, filterValue, tint) => {
    const value = row[stage];
    if (!value) {
      return <span className="text-slate-300">—</span>;
    }
    return (
      <button
        onClick={() => onDrill(drillValue, filterValue)}
        className={`font-semibold tabular-nums hover:underline ${tint}`}
        title="Show these restaurants in the table below"
      >
        {value}
      </button>
    );
  };

  // kind: "metro" (rollup header), "city" (indented child), "totals".
  const renderRow = (row, drillValue, kind) => (
    <tr
      key={`${kind}:${row.label}`}
      className={
        kind === "totals"
          ? "border-t-2 border-slate-300 bg-slate-50 font-semibold"
          : kind === "metro"
            ? "border-t border-slate-200 bg-slate-50/60"
            : "border-t border-slate-100"
      }
    >
      <td
        className={`py-2 pr-3 ${
          kind === "city"
            ? "pl-5 text-slate-600"
            : "font-semibold text-slate-800"
        }`}
      >
        {row.label}
      </td>
      <td className="py-2 pr-3">
        <div className="flex items-center gap-2">
          <div className="h-2 w-28 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-emerald-500"
              style={{ width: `${pct(row)}%` }}
            />
          </div>
          <span className="w-9 text-right text-xs tabular-nums text-slate-500">
            {pct(row)}%
          </span>
        </div>
      </td>
      <td className="py-2 pr-3 text-right">
        <button
          onClick={() => onDrill(drillValue, "all")}
          className="font-semibold tabular-nums text-slate-700 hover:underline"
        >
          {row.total}
        </button>
      </td>
      {COVERAGE_STAGES.map(([stage, , filterValue, tint]) => (
        <td key={stage} className="py-2 pr-3 text-right">
          {countCell(row, drillValue, stage, filterValue, tint)}
        </td>
      ))}
    </tr>
  );

  return (
    <section className="mb-6 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-bold text-slate-900">
            Coverage by area
          </h2>
          <p className="text-xs text-slate-500">
            Pipeline progress per metro area and city — click any count to
            drill into the table below. Active consumer venues only.
          </p>
        </div>
        <button
          onClick={() => setOpen((v) => !v)}
          className="text-xs font-semibold text-slate-500 hover:text-slate-800"
        >
          {open ? "Hide" : `Show (${metros.length} metro areas)`}
        </button>
      </div>
      {open && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="text-left text-xs font-bold uppercase tracking-wide text-slate-400">
                <th className="py-1 pr-3">Metro / city</th>
                <th className="py-1 pr-3">Progress</th>
                <th className="py-1 pr-3 text-right">Total</th>
                {COVERAGE_STAGES.map(([stage, label]) => (
                  <th key={stage} className="py-1 pr-3 text-right">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {metros.map(({ row, cities }) => {
                const standalone =
                  cities.length === 1 && cities[0].label === row.label;
                return (
                  <Fragment key={row.label}>
                    {renderRow(row, `metro:${row.label}`, "metro")}
                    {!standalone &&
                      cities.map((city) =>
                        renderRow(city, city.label, "city")
                      )}
                  </Fragment>
                );
              })}
              {metros.length > 1 && renderRow(totals, "all", "totals")}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// Tier-0 dish audit: deterministic sanity checks over stored classifications
// (implausible calories, self-contradicting verdicts, lost-decimal prices).
// Zero LLM tokens, swept on demand. Self-contained: fetches, dismisses, and
// applies one-click fixes against /api/dish-audit.
const AUDIT_SEVERITY_STYLE = {
  high: "bg-rose-100 text-rose-800",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-slate-100 text-slate-600",
};

function DishAuditPanel({ onDataChanged }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null); // last sweep result
  const [busy, setBusy] = useState(null); // `${dish_id}:${code}` while acting
  const [error, setError] = useState(null);

  async function sweep() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/dish-audit");
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `Audit failed (${res.status})`);
      setData(body);
      setOpen(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function dismiss(f) {
    setBusy(`${f.dish_id}:${f.code}`);
    setError(null);
    try {
      const res = await fetch(`/api/dish-audit/${f.dish_id}/dismiss`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: f.code, fingerprint: f.fingerprint }),
      });
      if (!res.ok) throw new Error((await res.json()).error || "Dismiss failed");
      setData((d) => ({
        ...d,
        findings: d.findings.filter(
          (x) => !(x.dish_id === f.dish_id && x.code === f.code)
        ),
        count: d.count - 1,
      }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  async function applyFix(f) {
    setBusy(`${f.dish_id}:${f.code}`);
    setError(null);
    try {
      const res = await fetch(`/api/dish-audit/${f.dish_id}/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ field: f.field, value: f.suggested }),
      });
      if (!res.ok) throw new Error((await res.json()).error || "Apply failed");
      setData((d) => ({
        ...d,
        findings: d.findings.filter(
          (x) => !(x.dish_id === f.dish_id && x.code === f.code)
        ),
        count: d.count - 1,
      }));
      onDataChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  const findings = data?.findings || [];

  return (
    <section className="mb-6 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-bold text-slate-900">
            Dish data audit
          </h2>
          <p className="text-xs text-slate-500">
            Deterministic sanity checks over stored dishes — implausible
            calories, self-contradicting verdicts, mis-parsed prices. No
            tokens.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data && (
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-bold text-slate-700">
              {data.count} finding{data.count === 1 ? "" : "s"}
            </span>
          )}
          <button
            onClick={sweep}
            disabled={loading}
            className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-bold text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {loading ? "Sweeping…" : data ? "Re-run audit" : "Run audit"}
          </button>
          {data && (
            <button
              onClick={() => setOpen((v) => !v)}
              className="text-xs font-semibold text-slate-500 hover:text-slate-800"
            >
              {open ? "Hide" : "Show"}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="mt-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      {open && data && (
        <div className="mt-3 space-y-2">
          {findings.length === 0 && (
            <div className="rounded-lg bg-emerald-50 px-3 py-2 text-sm font-medium text-emerald-700">
              No dish data problems found. 🎉
            </div>
          )}
          {findings.map((f) => (
            <div
              key={`${f.dish_id}:${f.code}`}
              className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-slate-100 bg-slate-50/60 p-3 text-sm"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${
                      AUDIT_SEVERITY_STYLE[f.severity] || AUDIT_SEVERITY_STYLE.low
                    }`}
                  >
                    {f.severity}
                  </span>
                  <span className="font-semibold text-slate-800">
                    {f.restaurant_name}
                  </span>
                </div>
                <div className="mt-0.5 text-slate-700">{f.message}</div>
                {f.evidence && (
                  <div className="mt-0.5 text-xs text-slate-400">{f.evidence}</div>
                )}
              </div>
              <div className="flex shrink-0 gap-2">
                {f.suggested && f.field && (
                  <button
                    onClick={() => applyFix(f)}
                    disabled={busy !== null}
                    className="rounded-lg border border-emerald-300 px-3 py-1.5 text-xs font-bold text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-40"
                    title={`Set ${f.field} to ${f.suggested}`}
                  >
                    {busy === `${f.dish_id}:${f.code}`
                      ? "…"
                      : `Fix → ${f.suggested}`}
                  </button>
                )}
                <button
                  onClick={() => dismiss(f)}
                  disabled={busy !== null}
                  className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                  title="Reviewed and fine — hide until this value changes"
                >
                  Dismiss
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function Admin() {
  const [restaurants, setRestaurants] = useState([]);
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [enriching, setEnriching] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [notice, setNotice] = useState(null);
  const [query, setQuery] = useState("");
  const [operationalFilter, setOperationalFilter] = useState("all");
  const [areaFilter, setAreaFilter] = useState("all");
  const [groupBy, setGroupBy] = useState("none");
  const [sortBy, setSortBy] = useState("recent");
  const [tableView, setTableView] = useState("active"); // active | archived
  const [menuFor, setMenuFor] = useState(null); // restaurant whose menu is open
  const [historyFor, setHistoryFor] = useState(null); // menu-history modal
  const [deleteFor, setDeleteFor] = useState(null); // restaurant pending permanent deletion
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [deleting, setDeleting] = useState(false);
  // full = re-extract everything; auto = skip unchanged menus and classify
  // only the changes (delta) when a prior inventory exists.
  const [classifyMode, setClassifyMode] = useState("auto");
  // Concurrency: sequential when subscription quota is running low — one
  // restaurant either finishes or doesn't, instead of several dying mid-run.
  const [classifyParallel, setClassifyParallel] = useState(3);
  const [menuText, setMenuText] = useState(null);
  const [menuScore, setMenuScore] = useState(null);
  const [menuLoading, setMenuLoading] = useState(false);
  const [dishesFor, setDishesFor] = useState(null); // restaurant whose dishes are open
  const [rowBusy, setRowBusy] = useState(null); // {id, action} while a per-row job runs
  const [addOpen, setAddOpen] = useState(false);
  const [addNames, setAddNames] = useState("");
  const [resolving, setResolving] = useState(false);
  const [addResolved, setAddResolved] = useState(null); // [{query, candidates}]
  const [addSelections, setAddSelections] = useState({}); // query -> place_id | ""
  const [addIngest, setAddIngest] = useState(true);
  const [addClassify, setAddClassify] = useState(true);
  const [adding, setAdding] = useState(false);
  const [addResult, setAddResult] = useState(null);
  const [reports, setReports] = useState([]);
  const [ingestJob, setIngestJob] = useState(null); // live bulk-scrape status
  const [classifyJob, setClassifyJob] = useState(null); // live bulk-classify status
  const [classifying, setClassifying] = useState(false);
  const [menuQuality, setMenuQuality] = useState([]); // automated audit flags
  const [qualityOpen, setQualityOpen] = useState(false);
  const [qualityBusy, setQualityBusy] = useState(null);
  const [providerUsage, setProviderUsage] = useState(null); // subscription limits
  const [selectedIds, setSelectedIds] = useState([]);
  const [classifierProvider, setClassifierProvider] = useState("deepseek");

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      // Price the cost estimates against the provider currently selected in
      // the classify controls, so the pre-run number matches the button.
      const providerParam = encodeURIComponent(classifierProvider);
      const [rRes, cRes, reportRes, qualityRes, usageRes] = await Promise.all([
        fetch(`/api/restaurants?include_excluded=true&provider=${providerParam}`),
        fetch("/api/config"),
        fetch("/api/reports?status=open"),
        fetch("/api/menu-quality"),
        fetch("/api/provider-usage"),
      ]);
      if (!rRes.ok) throw new Error(`/api/restaurants ${rRes.status}`);
      const rData = await rRes.json();
      setRestaurants(rData.restaurants);
      setSelectedIds((current) =>
        current.filter((id) => rData.restaurants.some((restaurant) => restaurant.id === id))
      );
      if (cRes.ok) setConfig(await cRes.json());
      if (reportRes.ok) setReports((await reportRes.json()).reports || []);
      if (qualityRes.ok) setMenuQuality((await qualityRes.json()).findings || []);
      if (usageRes.ok) setProviderUsage(await usageRes.json());
    } catch (e) {
      setError(e.message || "Failed to load. Is the backend running on :5000?");
    } finally {
      setLoading(false);
    }
  }

  // Re-price cost estimates when the classify provider changes — refetch
  // just the restaurant list (estimates ride on it), skipping the initial
  // mount which loadData() already covers.
  const didMountRef = useRef(false);
  useEffect(() => {
    if (!didMountRef.current) {
      didMountRef.current = true;
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `/api/restaurants?include_excluded=true&provider=${encodeURIComponent(
            classifierProvider
          )}`
        );
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setRestaurants(data.restaurants);
      } catch {
        /* keep the existing estimates on a transient failure */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [classifierProvider]);

  useEffect(() => {
    loadData();
    // If a background scrape/classification is already running (including a
    // one-row reclassify), reconnect after a browser reload.
    (async () => {
      try {
        const res = await fetch("/api/ingest/status");
        const data = await res.json();
        if (data.running) {
          setIngesting(true);
          pollIngest();
        }
        const cRes = await fetch("/api/classify/status");
        const cData = await cRes.json();
        if (cData.running) {
          setClassifying(true);
          pollClassify();
        }
      } catch {
        /* backend not up yet; loadData surfaces that */
      }
    })();
  }, []);

  async function runDiscovery() {
    setDiscovering(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Discovery failed (${res.status})`);
      setNotice(
        `Discovery complete: ${data.discovered} restaurant(s) in ${
          config?.city || "the area"
        }. Total in DB: ${data.total_in_db}.`
      );
      await loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setDiscovering(false);
    }
  }

  // Bulk ingestion runs as a background job on the backend; poll its status
  // so the dashboard shows live scrape-by-scrape progress.
  async function pollIngest(label = "Menu ingestion") {
    for (;;) {
      let data;
      try {
        const res = await fetch("/api/ingest/status");
        data = await res.json();
      } catch {
        break; // backend went away; stop polling quietly
      }
      setIngestJob(data);
      if (!data.running) {
        if (data.error) setError(data.error);
        else if (data.summary) {
          setNotice(
            `${label}: ${data.summary.succeeded} scraped, ${data.summary.failed} failed ` +
              `(blocked / JS-rendered — photo-fallback candidates).`
          );
        }
        setIngestJob(null);
        await loadData();
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    setIngesting(false);
  }

  async function pollClassify() {
    for (;;) {
      let data;
      try {
        const res = await fetch("/api/classify/status");
        data = await res.json();
      } catch {
        break;
      }
      setClassifyJob(data);
      if (!data.running) {
        if (data.error) setError(data.error);
        else if (data.summary) {
          const s = data.summary;
          setNotice(
            `${s.cancelled ? "Classification stopped" : "Classification"}: ` +
              `${s.ok} restaurant(s), ${s.dishes} dishes, ` +
              (s.billing === "api"
                ? `~$${(s.cost ?? 0).toFixed(2)} API cost`
                : `via ${PROVIDER_LABELS[s.provider] || "subscription"}`) +
              (s.failed ? `, ${s.failed} failed` : "") +
              "."
          );
        }
        setClassifyJob(null);
        await loadData();
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    setClassifying(false);
  }

  async function runClassify() {
    setClassifying(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/classify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: classifierProvider,
          mode: classifyMode,
          parallel: classifyParallel,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Classify failed (${res.status})`);
      await pollClassify();
    } catch (e) {
      setError(e.message);
      setClassifying(false);
    }
  }

  async function runIngest(staleOnly = false) {
    setIngesting(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(staleOnly ? { stale_days: 30 } : {}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Ingest failed (${res.status})`);
      await pollIngest(staleOnly ? "Stale menu refresh" : "Menu ingestion");
    } catch (e) {
      setError(e.message);
      setIngesting(false);
    }
  }

  async function resolveReport(id) {
    const response = await fetch(`/api/reports/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "resolved" }),
    });
    if (response.ok) setReports((current) => current.filter((report) => report.id !== id));
  }

  async function reviewMenuQuality(finding, status) {
    setQualityBusy(finding.restaurant_id);
    setError(null);
    try {
      const response = await fetch(
        `/api/menu-quality/${finding.restaurant_id}/review`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, fingerprint: finding.fingerprint }),
        }
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not save review.");
      await loadData();
    } catch (reviewError) {
      setError(reviewError.message);
    } finally {
      setQualityBusy(null);
    }
  }

  async function reopenMenuQuality(finding) {
    setQualityBusy(finding.restaurant_id);
    setError(null);
    try {
      const response = await fetch(
        `/api/menu-quality/${finding.restaurant_id}/review`,
        { method: "DELETE" }
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not reopen warning.");
      await loadData();
    } catch (reviewError) {
      setError(reviewError.message);
    } finally {
      setQualityBusy(null);
    }
  }

  // Export the consumer snapshots and push them — GitHub Pages redeploys,
  // so the phone-reachable public site catches up with local data.
  async function runPublish() {
    setPublishing(true);
    setNotice(null);
    try {
      const res = await fetch("/api/publish", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Publish failed (${res.status})`);
      setNotice(data.message);
    } catch (e) {
      setNotice(`Publish failed: ${e.message}`);
    } finally {
      setPublishing(false);
    }
  }

  async function runEnrich() {
    setEnriching(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/enrich", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ all: true }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Enrich failed (${res.status})`);
      setNotice(
        `Google enrichment: ${data.veg_yes} vegetarian-friendly, ` +
          `${data.veg_unknown} unknown; ${data.with_editorial} editorial summaries; ` +
          `${data.with_rating} ratings.`
      );
      await loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setEnriching(false);
    }
  }

  async function openMenu(r) {
    setMenuFor(r);
    setMenuText(null);
    setMenuLoading(true);
    try {
      const res = await fetch(`/api/restaurants/${r.id}/menu-text`);
      const data = await res.json();
      if (res.ok) {
        setMenuText(data.content);
        setMenuScore(data.menu_score);
      } else {
        setMenuText(`(${data.error || "no text"})`);
        setMenuScore(null);
      }
    } catch (e) {
      setMenuText(`(failed to load: ${e.message})`);
    } finally {
      setMenuLoading(false);
    }
  }

  // Step 1 of the add flow: resolve names to candidates — nothing written.
  async function resolveAddNames() {
    const names = addNames
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (names.length === 0) return;
    setResolving(true);
    setAddResult(null);
    try {
      const res = await fetch("/api/restaurants/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Resolve failed (${res.status})`);
      setAddResolved(data.resolved);
      // Preselect the best plausible match per name; weak-overlap-only
      // results start unselected so a wrong match needs a deliberate click.
      const selections = {};
      for (const entry of data.resolved) {
        const best = entry.candidates.find((c) => c.name_overlap);
        selections[entry.query] = best ? best.place_id : "";
      }
      setAddSelections(selections);
    } catch (e) {
      setAddResult({ error: e.message });
    } finally {
      setResolving(false);
    }
  }

  // Step 2: add exactly the confirmed places, running only the chosen stages.
  async function confirmAdd() {
    const places = [];
    for (const entry of addResolved || []) {
      const chosen = entry.candidates.find(
        (c) => c.place_id === addSelections[entry.query]
      );
      if (chosen) places.push(chosen);
    }
    if (places.length === 0) return;
    setAdding(true);
    setAddResult(null);
    try {
      const res = await fetch("/api/restaurants/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          places,
          ingest: addIngest,
          classify: addIngest && addClassify,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Add failed (${res.status})`);
      setAddResult(data);
      setAddResolved(null);
      await loadData();
    } catch (e) {
      setAddResult({ error: e.message });
    } finally {
      setAdding(false);
    }
  }

  async function runRowAction(r, action) {
    // Classification uses the shared background job so progress survives a
    // browser refresh; ingestion remains a synchronous one-row debug action.
    setRowBusy({ id: r.id, action });
    if (action === "classify") setClassifying(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch(`/api/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          restaurant_id: r.id,
          // The mode toggle decides: full re-extraction (classifier changed,
          // menu didn't) vs changes-only (skip unchanged, delta the rest).
          ...(action === "classify"
            ? { provider: classifierProvider, mode: classifyMode }
            : {}),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `${action} failed (${res.status})`);
      if (action === "classify") {
        await pollClassify();
      } else {
        setNotice(
          `Rescraped ${r.name}: ${data.succeeded ? "menu found" : "no menu found"}${
            data.failures?.[0] ? ` — ${data.failures[0].error}` : ""
          }`
        );
        await loadData();
      }
    } catch (e) {
      setError(`${r.name}: ${e.message}`);
      if (action === "classify") setClassifying(false);
    } finally {
      setRowBusy(null);
    }
  }

  async function toggleArchived(restaurant) {
    const archived = !restaurant.archived;
    const response = await fetch(`/api/restaurants/${restaurant.id}/archived`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archived }),
    });
    if (!response.ok) return;
    setRestaurants((current) =>
      current.map((item) =>
        item.id === restaurant.id ? { ...item, archived: archived ? 1 : 0 } : item
      )
    );
  }

  async function permanentlyDeleteRestaurant() {
    if (!deleteFor || deleteConfirm !== deleteFor.name) return;
    setDeleting(true);
    setError(null);
    try {
      const response = await fetch(`/api/restaurants/${deleteFor.id}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm_name: deleteConfirm }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Restaurant deletion failed.");
      const deleted = data.deleted;
      setDeleteFor(null);
      setDeleteConfirm("");
      setSelectedIds((current) => current.filter((id) => id !== deleted.id));
      setNotice(
        `Permanently deleted ${deleted.name}: ${deleted.dishes} dishes, ` +
          `${deleted.classifications} classifications, and ${deleted.menu_versions} menu versions removed.`
      );
      await loadData();
    } catch (deleteError) {
      setError(deleteError.message);
    } finally {
      setDeleting(false);
    }
  }

  async function toggleVisibility(restaurant) {
    const hidden = !Boolean(restaurant.consumer_hidden);
    const response = await fetch(`/api/restaurants/${restaurant.id}/visibility`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hidden }),
    });
    if (!response.ok) return;
    setRestaurants((current) =>
      current.map((item) =>
        item.id === restaurant.id
          ? {
              ...item,
              consumer_hidden: hidden ? 1 : 0,
              is_consumer_venue: !hidden,
            }
          : item
      )
    );
    if (hidden) {
      setSelectedIds((current) => current.filter((id) => id !== restaurant.id));
    }
  }

  async function stopClassify() {
    try {
      const res = await fetch("/api/classify/stop", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Stop failed (${res.status})`);
      setClassifyJob((current) =>
        current ? { ...current, cancel_requested: true } : current
      );
    } catch (e) {
      setError(e.message);
    }
  }

  async function stopIngest() {
    try {
      const res = await fetch("/api/ingest/stop", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Stop failed (${res.status})`);
      setIngestJob((current) =>
        current ? { ...current, cancel_requested: true } : current
      );
      // The stop also kills any hung headless browser so a wedged scrape
      // unblocks; surface what it did.
      setNotice(
        data.browsers
          ? `Stopping scrape — ${data.note}. Menus already scraped are kept.`
          : "Stopping scrape after the current restaurant. Scraped menus are kept."
      );
    } catch (e) {
      setError(e.message);
    }
  }

  async function toggleRefreshEnabled(restaurant) {
    const enabled = !Boolean(restaurant.refresh_enabled);
    const response = await fetch(
      `/api/restaurants/${restaurant.id}/refresh-enabled`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      }
    );
    if (!response.ok) return;
    setRestaurants((current) =>
      current.map((item) =>
        item.id === restaurant.id
          ? { ...item, refresh_enabled: enabled ? 1 : 0 }
          : item
      )
    );
    if (!enabled) {
      setSelectedIds((current) => current.filter((id) => id !== restaurant.id));
    }
  }

  // Data-quality flags: places with no website (scraping fallback needed)
  // and how many have menu text scraped so far.
  const noWebsite = useMemo(
    () => restaurants.filter((r) => !r.website_url).length,
    [restaurants]
  );
  const withMenuText = useMemo(
    () => restaurants.filter((r) => r.has_menu_text).length,
    [restaurants]
  );
  const vegFriendly = useMemo(
    () => restaurants.filter((r) => r.serves_vegetarian === 1).length,
    [restaurants]
  );
  const totalVeganOptions = useMemo(
    () => restaurants.reduce((sum, r) => sum + (r.vegan_options || 0), 0),
    [restaurants]
  );
  const totalVeganSides = useMemo(
    () => restaurants.reduce((sum, r) => sum + (r.vegan_sides || 0), 0),
    [restaurants]
  );
  const staleMenus = useMemo(
    () => restaurants.filter((restaurant) => isMenuStale(restaurant.menu_fetched_at)).length,
    [restaurants]
  );
  const unclassified = useMemo(
    () =>
      restaurants.filter((r) => r.has_menu_text && (r.dish_count || 0) === 0)
        .length,
    [restaurants]
  );
  // Pre-run cost estimates (from menu size): everything vs. only-new.
  const classifyCostAll = useMemo(
    () =>
      restaurants.reduce((sum, r) => sum + (r.classify_estimate || 0), 0),
    [restaurants]
  );
  const classifyCostNew = useMemo(
    () =>
      restaurants
        .filter((r) => r.has_menu_text && (r.dish_count || 0) === 0)
        .reduce((sum, r) => sum + (r.classify_estimate || 0), 0),
    [restaurants]
  );
  const resolvedClassifierProvider =
    classifierProvider === "auto"
      ? config?.classifier?.resolved
      : classifierProvider;
  const classifierUsesApi = ["anthropic", "deepseek"].includes(
    resolvedClassifierProvider
  );
  const classifierProviderLabel =
    PROVIDER_LABELS[resolvedClassifierProvider] || "No provider available";

  const archivedCount = useMemo(
    () => restaurants.filter((restaurant) => restaurant.archived).length,
    [restaurants]
  );

  const activeMenuQuality = useMemo(
    () => menuQuality.filter((finding) => !finding.review_status),
    [menuQuality]
  );
  const knownMenuIssues = useMemo(
    () => menuQuality.filter((finding) => finding.review_status === "known_issue"),
    [menuQuality]
  );
  const verifiedMenus = useMemo(
    () => menuQuality.filter((finding) => finding.review_status === "verified"),
    [menuQuality]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const qualityIds = new Set(activeMenuQuality.map((finding) => finding.restaurant_id));
    const list = restaurants.filter((restaurant) => {
      // Archived rows live on their own page; everything else never sees them.
      if (Boolean(restaurant.archived) !== (tableView === "archived")) return false;
      if (areaFilter !== "all") {
        const area = restaurant.area || "Unknown";
        const metro = restaurant.metro || area;
        if (areaFilter.startsWith("metro:")) {
          if (metro !== areaFilter.slice(6)) return false;
        } else if (area !== areaFilter) {
          return false;
        }
      }
      if (
        q &&
        !restaurant.name?.toLowerCase().includes(q) &&
        !restaurant.address?.toLowerCase().includes(q)
      ) {
        return false;
      }
      if (operationalFilter === "refresh_on") return Boolean(restaurant.refresh_enabled);
      if (operationalFilter === "refresh_paused") return !restaurant.refresh_enabled;
      if (operationalFilter === "needs_menu") {
        return restaurant.is_consumer_venue && restaurant.website_url && !restaurant.has_menu_text;
      }
      if (operationalFilter === "scrape_failing") {
        return (
          restaurant.is_consumer_venue &&
          restaurant.website_url &&
          !restaurant.has_menu_text &&
          (restaurant.crawl_failures || 0) > 0
        );
      }
      if (operationalFilter === "ready_to_classify") {
        return restaurant.has_menu_text && (restaurant.dish_count || 0) === 0;
      }
      if (operationalFilter === "classified") return (restaurant.dish_count || 0) > 0;
      if (operationalFilter === "stale") return isMenuStale(restaurant.menu_fetched_at);
      if (operationalFilter === "excluded") return !restaurant.is_consumer_venue;
      if (operationalFilter === "no_website") return !restaurant.website_url;
      if (operationalFilter === "quality") return qualityIds.has(restaurant.id);
      return true;
    });

    // Nulls always sink to the bottom, whatever the direction — "sort by
    // last classified" should read as a clean timeline, not nulls-first.
    const time = (value) => (value ? new Date(value).getTime() : null);
    const byNullable = (extract, direction) => (a, b) => {
      const va = extract(a);
      const vb = extract(b);
      if (va == null && vb == null) return a.name.localeCompare(b.name);
      if (va == null) return 1;
      if (vb == null) return -1;
      return direction * (va - vb) || a.name.localeCompare(b.name);
    };
    if (sortBy === "name") {
      return [...list].sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    }
    if (sortBy === "classified_desc") {
      return [...list].sort(byNullable((r) => time(r.last_classified_at), -1));
    }
    if (sortBy === "classified_asc") {
      return [...list].sort(byNullable((r) => time(r.last_classified_at), 1));
    }
    if (sortBy === "scraped_desc") {
      return [...list].sort(byNullable((r) => time(r.menu_fetched_at), -1));
    }
    if (sortBy === "menu_size") {
      return [...list].sort(byNullable((r) => r.menu_chars, -1));
    }
    if (sortBy === "vegan_meals") {
      return [...list].sort(byNullable((r) => r.vegan_options ?? 0, -1));
    }
    return list; // "recent" — the API's last_scraped_at order
  }, [restaurants, query, operationalFilter, areaFilter, activeMenuQuality, tableView, sortBy]);

  const groupedFiltered = useMemo(() => {
    if (groupBy === "none") return [{ label: "All restaurants", items: filtered }];

    const labelFor = (restaurant) => {
      if (groupBy === "area") {
        return restaurant.area || "Unknown";
      }
      if (groupBy === "metro") {
        return restaurant.metro || restaurant.area || "Unknown";
      }
      if (groupBy === "refresh") {
        return restaurant.refresh_enabled ? "Refresh enabled" : "Refresh paused";
      }
      if (groupBy === "freshness") {
        if (!restaurant.has_menu_text) return "No menu stored";
        return isMenuStale(restaurant.menu_fetched_at)
          ? "Menu needs refresh"
          : "Menu current";
      }
      if (groupBy === "classification_age") {
        return classificationAgeGroup(restaurant.last_classified_at);
      }
      if (!restaurant.is_consumer_venue) return "Excluded from Explore";
      if (!restaurant.website_url) return "No website";
      if (!restaurant.has_menu_text) return "Needs menu scrape";
      if ((restaurant.dish_count || 0) === 0) return "Ready to classify";
      return "Classified";
    };

    const groups = new Map();
    for (const restaurant of filtered) {
      const label = labelFor(restaurant);
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(restaurant);
    }
    const result = [...groups.entries()].map(([label, items]) => ({
      label,
      items:
        // An explicit sort choice wins inside groups too; the default
        // classification-age grouping keeps its newest-first convention.
        groupBy === "classification_age" && sortBy === "recent"
          ? [...items].sort(
              (a, b) =>
                new Date(b.last_classified_at || 0).getTime() -
                new Date(a.last_classified_at || 0).getTime()
            )
          : items,
    }));
    if (groupBy === "classification_age") {
      const order = [
        "Never classified",
        "Classified over 30 days ago",
        "Classified 8–30 days ago",
        "Classified in the past 7 days",
      ];
      result.sort((a, b) => order.indexOf(a.label) - order.indexOf(b.label));
    }
    if (groupBy === "refresh") {
      const order = ["Refresh enabled", "Refresh paused"];
      result.sort((a, b) => order.indexOf(a.label) - order.indexOf(b.label));
    }
    if (groupBy === "area" || groupBy === "metro") {
      result.sort(
        (a, b) =>
          b.items.length - a.items.length || a.label.localeCompare(b.label)
      );
    }
    return result;
  }, [filtered, groupBy, sortBy]);

  // Distinct areas (active rows) nested under their metro, biggest first —
  // drives the area dropdown's optgroups.
  const areaOptions = useMemo(() => {
    const metros = new Map();
    for (const restaurant of restaurants) {
      if (restaurant.archived) continue;
      const area = restaurant.area || "Unknown";
      const metro = restaurant.metro || area;
      if (!metros.has(metro)) metros.set(metro, new Map());
      const cities = metros.get(metro);
      cities.set(area, (cities.get(area) || 0) + 1);
    }
    return [...metros.entries()]
      .map(([metro, cities]) => ({
        metro,
        count: [...cities.values()].reduce((a, b) => a + b, 0),
        cities: [...cities.entries()].sort(
          (a, b) => b[1] - a[1] || a[0].localeCompare(b[0])
        ),
      }))
      .sort((a, b) => b.count - a.count || a.metro.localeCompare(b.metro));
  }, [restaurants]);

  // Coverage-panel drill-down: point every table control at one bucket.
  const drillCoverage = (area, opFilter) => {
    setTableView("active");
    setAreaFilter(area);
    setOperationalFilter(opFilter);
    setGroupBy("none");
    setQuery("");
  };

  const selectableFiltered = useMemo(
    () =>
      filtered.filter(
        (restaurant) =>
          restaurant.refresh_enabled && restaurant.is_consumer_venue
      ),
    [filtered]
  );
  const selectedRestaurants = useMemo(() => {
    const selected = new Set(selectedIds);
    return restaurants.filter((restaurant) => selected.has(restaurant.id));
  }, [restaurants, selectedIds]);
  const selectedScrapeIds = useMemo(
    () =>
      selectedRestaurants
        .filter((restaurant) => restaurant.website_url)
        .map((restaurant) => restaurant.id),
    [selectedRestaurants]
  );
  const selectedClassifyIds = useMemo(
    () =>
      selectedRestaurants
        .filter((restaurant) => restaurant.has_menu_text)
        .map((restaurant) => restaurant.id),
    [selectedRestaurants]
  );
  const selectedClassifyCost = useMemo(
    () =>
      selectedRestaurants
        .filter((restaurant) => restaurant.has_menu_text)
        .reduce((sum, restaurant) => sum + (restaurant.classify_estimate || 0), 0),
    [selectedRestaurants]
  );
  const selectedClassifyChars = useMemo(
    () =>
      selectedRestaurants
        .filter((restaurant) => restaurant.has_menu_text)
        .reduce((sum, restaurant) => sum + (restaurant.menu_chars || 0), 0),
    [selectedRestaurants]
  );
  const allFilteredSelected =
    selectableFiltered.length > 0 &&
    selectableFiltered.every((restaurant) => selectedIds.includes(restaurant.id));

  function toggleSelected(id) {
    setSelectedIds((current) =>
      current.includes(id)
        ? current.filter((value) => value !== id)
        : [...current, id]
    );
  }

  function toggleAllFiltered() {
    const visibleIds = selectableFiltered.map((restaurant) => restaurant.id);
    setSelectedIds((current) => {
      if (visibleIds.every((id) => current.includes(id))) {
        return current.filter((id) => !visibleIds.includes(id));
      }
      return [...new Set([...current, ...visibleIds])];
    });
  }

  function toggleGroup(items) {
    const ids = items
      .filter((restaurant) => restaurant.refresh_enabled && restaurant.is_consumer_venue)
      .map((restaurant) => restaurant.id);
    setSelectedIds((current) => {
      if (ids.every((id) => current.includes(id))) {
        return current.filter((id) => !ids.includes(id));
      }
      return [...new Set([...current, ...ids])];
    });
  }

  async function runSelectedIngest() {
    if (selectedScrapeIds.length === 0) return;
    setIngesting(true);
    setNotice(null);
    setError(null);
    try {
      const response = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ restaurant_ids: selectedScrapeIds }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Selected scrape failed to start.");
      setSelectedIds([]);
      await pollIngest("Selected menu scrape");
    } catch (error) {
      setError(error.message);
      setIngesting(false);
    }
  }

  async function runSelectedClassify() {
    if (selectedClassifyIds.length === 0) return;
    setClassifying(true);
    setNotice(null);
    setError(null);
    try {
      const response = await fetch("/api/classify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          restaurant_ids: selectedClassifyIds,
          provider: classifierProvider,
          mode: classifyMode,
          parallel: classifyParallel,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Selected classification failed to start.");
      setSelectedIds([]);
      await pollClassify();
    } catch (error) {
      setError(error.message);
      setClassifying(false);
    }
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <div className="w-full max-w-none px-2 py-4 sm:px-6 sm:py-8 lg:px-8">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-lg font-bold sm:text-2xl">VeganFind — Pipeline Dashboard</h1>
            <p className="text-sm text-slate-500">
              Discovery · menu scraping · dish classification
              {config?.city ? ` · ${config.city}, FL` : ""}
            </p>
          </div>
          <div className="flex gap-2 max-sm:snap-x max-sm:overflow-x-auto max-sm:pb-1 max-sm:[&>*]:shrink-0 sm:flex-wrap sm:justify-end">
            <button
              onClick={runPublish}
              disabled={publishing}
              className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              title="Export the current restaurants/dishes as static snapshots, commit, and push — the public site redeploys with your latest data in ~2 minutes"
            >
              {publishing ? "Publishing…" : "⬆ Publish to live site"}
            </button>
            <button
              onClick={() => {
                setAddOpen(true);
                setAddResult(null);
                setAddResolved(null);
                setAddSelections({});
              }}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50"
              title="Add restaurants by name — resolves via Google, then scrapes"
            >
              + Add restaurants
            </button>
            <button
              onClick={runEnrich}
              disabled={enriching || !config?.has_api_key}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
              title="Refresh ratings, current opening status, and Google food signals for every restaurant"
            >
              {enriching ? "Refreshing…" : "Refresh Google data"}
            </button>
            <button
              onClick={() => runIngest(true)}
              disabled={ingesting || staleMenus === 0}
              className="rounded-lg border border-amber-300 px-4 py-2 text-sm font-semibold text-amber-800 shadow-sm transition hover:bg-amber-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
              title="Re-scrape menus last checked more than 30 days ago"
            >
              Refresh stale ({staleMenus})
            </button>
            <select
              value={classifierProvider}
              onChange={(event) => setClassifierProvider(event.target.value)}
              disabled={classifying}
              className="rounded-lg border border-violet-300 bg-white px-3 py-2 text-sm font-semibold text-violet-800 shadow-sm disabled:cursor-not-allowed disabled:text-slate-400"
              aria-label="Classification provider"
              title="Choose how menu classifications are generated"
            >
              <option
                value="deepseek"
                disabled={!config?.classifier?.providers?.deepseek?.available}
              >
                DeepSeek
              </option>
            </select>
            <select
              value={classifyMode}
              onChange={(event) => setClassifyMode(event.target.value)}
              disabled={classifying}
              className="rounded-lg border border-violet-300 bg-white px-3 py-2 text-sm font-semibold text-violet-800 shadow-sm disabled:cursor-not-allowed disabled:text-slate-400"
              aria-label="Reclassification mode"
              title="Changes only: skip menus whose text is unchanged and classify only the differences (cheapest). Full: re-extract everything with the current classifier — use after classifier changes, since unchanged menus keep old verdicts otherwise."
            >
              <option value="auto">Changes only</option>
              <option value="full">Full re-extraction</option>
            </select>
            <select
              value={classifyParallel}
              onChange={(event) => setClassifyParallel(Number(event.target.value))}
              disabled={classifying}
              className="rounded-lg border border-violet-300 bg-white px-3 py-2 text-sm font-semibold text-violet-800 shadow-sm disabled:cursor-not-allowed disabled:text-slate-400"
              aria-label="Classification concurrency"
              title="How many restaurants classify at once. One at a time when your subscription window is nearly used up — each restaurant either completes or doesn't, instead of several dying mid-run together; the finished ones are saved either way."
            >
              <option value={1}>1 at a time</option>
              <option value={2}>2 in parallel</option>
              <option value={3}>3 in parallel</option>
              <option value={6}>6 in parallel</option>
            </select>
            <button
              onClick={runClassify}
              disabled={classifying || unclassified === 0}
              className="rounded-lg border border-violet-400 px-4 py-2 text-sm font-semibold text-violet-700 shadow-sm transition hover:bg-violet-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
              title={
                classifierUsesApi
                  ? `Classify new menus with ${classifierProviderLabel} — est ~$${classifyCostNew.toFixed(2)} API cost`
                  : `Classify new menus with your ${classifierProviderLabel}`
              }
            >
              {classifying
                ? "Classifying…"
                : `⚡ Classify new (${unclassified}${
                    unclassified > 0 && classifierUsesApi
                      ? ` · ~$${classifyCostNew.toFixed(2)}`
                      : ""
                  })`}
            </button>
            <button
              onClick={() => runIngest(false)}
              disabled={ingesting || discovering}
              className="rounded-lg border border-emerald-600 px-4 py-2 text-sm font-semibold text-emerald-700 shadow-sm transition hover:bg-emerald-50 disabled:cursor-not-allowed disabled:border-slate-300 disabled:text-slate-400"
              title="Scrapes menu text for restaurants that don't have it yet"
            >
              {ingesting ? "Ingesting…" : "Ingest menus"}
            </button>
            <button
              onClick={runDiscovery}
              disabled={discovering || ingesting || !config?.has_api_key}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              title={
                config && !config.has_api_key
                  ? "GOOGLE_PLACES_API_KEY not set in .env"
                  : "Runs ~49 Places API calls"
              }
            >
              {discovering ? "Discovering…" : "Run discovery"}
            </button>
          </div>
          <p
            className="w-full text-right text-xs text-slate-400"
            title="Current classification provider and estimated spend"
          >
            Classifier: {classifierProviderLabel}
            {classifierUsesApi &&
              ` · re-running all ${withMenuText} ≈ $${classifyCostAll.toFixed(2)}`}
          </p>
        </header>

        {config && !config.has_api_key && (
          <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            No <code>GOOGLE_PLACES_API_KEY</code> in <code>.env</code> — discovery
            is disabled. You can still browse existing data.
          </div>
        )}
        {notice && (
          <div className="mb-4 rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
            {notice}
          </div>
        )}
        {error && (
          <div className="mb-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <JobProgressPanel
          job={ingestJob}
          title="Scraping menus"
          onStop={stopIngest}
        />
        <JobProgressPanel
          job={classifyJob}
          title="Classifying dishes"
          onStop={stopClassify}
        />

        <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard label="Restaurants" value={restaurants.length} />
          <StatCard
            label="Real menus found"
            value={`${withMenuText} / ${restaurants.length - noWebsite}`}
            hint="of restaurants with a website"
          />
          <StatCard
            label="Vegetarian-friendly"
            value={vegFriendly}
            hint="per Google"
          />
          <StatCard
            label="Vegan meals found"
            value={totalVeganOptions}
            hint={`${totalVeganSides} sides/small plates tracked separately`}
          />
        </div>

        {providerUsage && (
          <section className="mb-6 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-bold text-slate-900">
                Subscription limits
              </h2>
              <span className="text-xs text-slate-400">
                how much classification budget is left · refreshes with the page
              </span>
            </div>
            <div className="space-y-3">
              {["claude", "codex", "deepseek"].map((name) => {
                const usage = providerUsage[name];
                if (name === "deepseek" && !usage?.available) return null;
                return (
                  <div key={name}>
                    <div className="text-xs font-semibold text-slate-700">
                      {PROVIDER_LABELS[name]}
                      {usage?.plan ? (
                        <span className="ml-1 font-normal text-slate-400">
                          · {usage.plan}
                        </span>
                      ) : null}
                      {usage?.as_of ? (
                        <span
                          className="ml-1 font-normal text-slate-400"
                          title="Read from local Codex session logs — updates whenever Codex runs"
                        >
                          · as of{" "}
                          {new Date(usage.as_of).toLocaleTimeString([], {
                            hour: "numeric",
                            minute: "2-digit",
                          })}
                        </span>
                      ) : null}
                    </div>
                    {usage?.available ? (
                      usage.windows ? (
                        <div className="mt-1 space-y-1">
                          {usage.windows.map((w) => (
                            <UsageBar key={w.id} window={w} />
                          ))}
                        </div>
                      ) : (
                        // Prepaid wallet (DeepSeek): dollars left, not a window.
                        <div
                          className={`mt-0.5 text-xs font-semibold ${
                            usage.balance != null && usage.balance < 0.5
                              ? "text-amber-700"
                              : "text-slate-600"
                          }`}
                        >
                          {usage.balance != null
                            ? `$${usage.balance.toFixed(2)} ${usage.currency} remaining`
                            : "Balance unknown"}
                          {usage.usable === false && " · top up to use"}
                        </div>
                      )
                    ) : (
                      <div className="mt-0.5 text-xs text-slate-400">
                        {usage?.reason || "Usage unknown."}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}

        <ScrapeFailuresPanel />
        <DishAuditPanel onDataChanged={loadData} />

        {menuQuality.length > 0 && (
          <section className="mb-6 rounded-xl border border-orange-200 bg-orange-50 p-4">
            <button
              onClick={() => setQualityOpen((v) => !v)}
              className="flex w-full items-center justify-between text-left"
            >
              <h2 className="font-bold text-orange-950">
                Menu quality warnings
                <span className="ml-2 text-xs font-medium text-orange-700">
                  automated audit — likely false or incomplete menus
                </span>
              </h2>
              <span className="rounded-full bg-orange-200 px-2 py-0.5 text-xs font-bold text-orange-900">
                {activeMenuQuality.length} active
                {knownMenuIssues.length > 0 && ` · ${knownMenuIssues.length} known`}{" "}
                {qualityOpen ? "▾" : "▸"}
              </span>
            </button>
            {qualityOpen && (
              <div className="mt-3 space-y-3">
                {activeMenuQuality.length === 0 && (
                  <div className="rounded-lg bg-white px-3 py-2 text-sm font-medium text-emerald-700 shadow-sm">
                    No unreviewed menu warnings.
                  </div>
                )}
                {activeMenuQuality.map((f) => (
                  <div key={f.restaurant_id} className="rounded-lg bg-white p-3 text-sm shadow-sm">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="font-bold text-slate-900">{f.name}</div>
                        <ul className="mt-0.5 text-xs text-orange-800">
                          {f.flags.map((flag) => <li key={flag}>• {flag}</li>)}
                        </ul>
                      </div>
                      <div className="flex gap-2 max-sm:snap-x max-sm:overflow-x-auto max-sm:pb-1 max-sm:[&>*]:shrink-0 sm:flex-wrap sm:justify-end">
                        <button
                          onClick={() => {
                            const restaurant = restaurants.find((item) => item.id === f.restaurant_id);
                            if (restaurant) runRowAction(restaurant, "ingest");
                          }}
                          disabled={rowBusy !== null || qualityBusy !== null}
                          className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          {rowBusy?.id === f.restaurant_id && rowBusy.action === "ingest"
                            ? "scraping…"
                            : "↻ Rescrape"}
                        </button>
                        <button
                          onClick={() => reviewMenuQuality(f, "verified")}
                          disabled={qualityBusy !== null || rowBusy !== null}
                          className="rounded-lg border border-emerald-300 px-3 py-1.5 text-xs font-bold text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-40"
                          title="The stored menu is correct; hide this warning until its content changes"
                        >
                          ✓ Menu is correct
                        </button>
                        <button
                          onClick={() => reviewMenuQuality(f, "known_issue")}
                          disabled={qualityBusy !== null || rowBusy !== null}
                          className="rounded-lg border border-amber-300 px-3 py-1.5 text-xs font-bold text-amber-800 hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-40"
                          title="The menu is incomplete or wrong, but there is no current solution"
                        >
                          No current solution
                        </button>
                      </div>
                    </div>
                  </div>
                ))}

                {knownMenuIssues.length > 0 && (
                  <details className="rounded-lg border border-amber-200 bg-amber-50/70 p-3">
                    <summary className="cursor-pointer text-sm font-bold text-amber-900">
                      Known issues — no current solution ({knownMenuIssues.length})
                    </summary>
                    <div className="mt-2 space-y-2">
                      {knownMenuIssues.map((finding) => (
                        <div key={finding.restaurant_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-white p-2.5 text-sm">
                          <div>
                            <div className="font-bold text-slate-800">{finding.name}</div>
                            <div className="text-xs text-amber-800">{finding.flags.join(" · ")}</div>
                          </div>
                          <button
                            onClick={() => reopenMenuQuality(finding)}
                            disabled={qualityBusy !== null}
                            className="text-xs font-bold text-amber-800 underline disabled:opacity-40"
                          >
                            Reopen warning
                          </button>
                        </div>
                      ))}
                    </div>
                  </details>
                )}

                {verifiedMenus.length > 0 && (
                  <details className="rounded-lg border border-emerald-200 bg-emerald-50/70 p-3">
                    <summary className="cursor-pointer text-sm font-bold text-emerald-800">
                      Verified correct ({verifiedMenus.length})
                    </summary>
                    <div className="mt-2 space-y-2">
                      {verifiedMenus.map((finding) => (
                        <div key={finding.restaurant_id} className="flex items-center justify-between gap-2 rounded-lg bg-white p-2.5 text-sm">
                          <span className="font-bold text-slate-800">{finding.name}</span>
                          <button
                            onClick={() => reopenMenuQuality(finding)}
                            disabled={qualityBusy !== null}
                            className="text-xs font-bold text-emerald-700 underline disabled:opacity-40"
                          >
                            Reopen warning
                          </button>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            )}
          </section>
        )}

        {reports.length > 0 && (
          <section className="mb-6 rounded-xl border border-amber-200 bg-amber-50 p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="font-bold text-amber-950">Correction reports</h2>
              <span className="rounded-full bg-amber-200 px-2 py-0.5 text-xs font-bold text-amber-900">{reports.length} open</span>
            </div>
            <div className="space-y-2">
              {reports.map((report) => (
                <div key={report.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-white p-3 text-sm shadow-sm">
                  <div>
                    <div className="font-bold text-slate-900">
                      {report.dish_name || "Restaurant report"} · {report.restaurant_name}
                    </div>
                    <div className="mt-0.5 text-xs capitalize text-amber-800">
                      {report.issue_type.replaceAll("_", " ")}
                      {report.note && ` — ${report.note}`}
                    </div>
                  </div>
                  <button onClick={() => resolveReport(report.id)} className="rounded-lg border border-emerald-300 px-3 py-1.5 text-xs font-bold text-emerald-700 hover:bg-emerald-50">
                    Mark resolved
                  </button>
                </div>
              ))}
            </div>
          </section>
        )}

        <AreaCoverage restaurants={restaurants} onDrill={drillCoverage} />

        <div className="mb-3 flex items-center gap-2 max-sm:overflow-x-auto max-sm:pb-1 max-sm:[&>*]:shrink-0 sm:flex-wrap">
          <div className="flex overflow-hidden rounded-lg border border-slate-300">
            {[
              ["active", `Active (${restaurants.length - archivedCount})`],
              ["archived", `Archived (${archivedCount})`],
              ["prospect", "🗺 Prospect"],
            ].map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTableView(key)}
                className={`px-3 py-2 text-sm font-semibold transition ${
                  tableView === key
                    ? "bg-slate-800 text-white"
                    : "bg-white text-slate-600 hover:bg-slate-50"
                }`}
                title={
                  key === "archived"
                    ? "Listings you'll never need (7-Eleven and friends): out of this table, Explore, and every bulk run — data kept"
                    : key === "prospect"
                      ? "Search any area on Google Places and pull restaurants into the pipeline — names only, scrape/classify later"
                      : "The working set"
                }
              >
                {label}
              </button>
            ))}
          </div>
          {tableView !== "prospect" && (
          <>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name or address…"
            className="w-full max-w-sm rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
          />
          <select
            value={areaFilter}
            onChange={(event) => setAreaFilter(event.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700"
            aria-label="Filter restaurants by area"
          >
            <option value="all">Area: All</option>
            {areaOptions.map(({ metro, count, cities }) =>
              cities.length === 1 && cities[0][0] === metro ? (
                <option key={metro} value={cities[0][0]}>
                  {metro} ({count})
                </option>
              ) : (
                <optgroup key={metro} label={metro}>
                  <option value={`metro:${metro}`}>
                    All of {metro} ({count})
                  </option>
                  {cities.map(([city, cityCount]) => (
                    <option key={city} value={city}>
                      {city} ({cityCount})
                    </option>
                  ))}
                </optgroup>
              )
            )}
          </select>
          <select
            value={operationalFilter}
            onChange={(event) => setOperationalFilter(event.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700"
            aria-label="Filter restaurants by operational status"
          >
            <option value="all">Filter: All restaurants</option>
            <option value="refresh_on">Refresh enabled</option>
            <option value="refresh_paused">Refresh paused</option>
            <option value="needs_menu">Needs menu scrape</option>
            <option value="scrape_failing">Scrape failing</option>
            <option value="ready_to_classify">Ready to classify</option>
            <option value="classified">Classified</option>
            <option value="stale">Stale menu</option>
            <option value="quality">Quality warning</option>
            <option value="excluded">Excluded from Explore</option>
            <option value="no_website">No website</option>
          </select>
          <select
            value={groupBy}
            onChange={(event) => setGroupBy(event.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700"
            aria-label="Group restaurants"
          >
            <option value="none">Group: None</option>
            <option value="metro">Group by metro area</option>
            <option value="area">Group by city</option>
            <option value="refresh">Group by refresh status</option>
            <option value="pipeline">Group by pipeline stage</option>
            <option value="freshness">Group by menu freshness</option>
            <option value="classification_age">Group by last classified</option>
          </select>
          <select
            value={sortBy}
            onChange={(event) => setSortBy(event.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700"
            aria-label="Sort restaurants"
          >
            <option value="recent">Sort: Recently scraped</option>
            <option value="name">Sort: Name A–Z</option>
            <option value="classified_desc">Sort: Last classified (newest)</option>
            <option value="classified_asc">Sort: Last classified (oldest)</option>
            <option value="scraped_desc">Sort: Menu fetched (newest)</option>
            <option value="menu_size">Sort: Menu size</option>
            <option value="vegan_meals">Sort: Vegan meals</option>
          </select>
          {(query || operationalFilter !== "all" || areaFilter !== "all" || groupBy !== "none" || sortBy !== "recent") && (
            <button
              onClick={() => {
                setQuery("");
                setOperationalFilter("all");
                setAreaFilter("all");
                setGroupBy("none");
                setSortBy("recent");
              }}
              className="text-xs font-semibold text-slate-500 hover:text-slate-800 hover:underline"
            >
              Reset view
            </button>
          )}
          <span className="ml-auto whitespace-nowrap text-sm text-slate-500">
            {filtered.length} shown
          </span>
          </>
          )}
        </div>

        {tableView === "prospect" ? (
          <ProspectPanel
            onAdded={loadData}
            config={config}
            defaultProvider={classifierProvider}
          />
        ) : (
        <>
        <div className="mb-3 flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 shadow-sm max-sm:overflow-x-auto max-sm:[&>*]:shrink-0 sm:flex-wrap">
          <span className="mr-1 text-sm font-semibold text-slate-700">
            {selectedIds.length} selected
          </span>
          <button
            onClick={runSelectedIngest}
            disabled={selectedScrapeIds.length === 0 || ingesting || classifying}
            className="rounded-lg border border-emerald-300 px-3 py-1.5 text-xs font-bold text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
          >
            Scrape menus ({selectedScrapeIds.length})
          </button>
          <button
            onClick={runSelectedClassify}
            disabled={selectedClassifyIds.length === 0 || classifying || ingesting}
            className="rounded-lg border border-violet-300 px-3 py-1.5 text-xs font-bold text-violet-700 hover:bg-violet-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
            title={
              classifierUsesApi
                ? `Estimated API cost: ~$${selectedClassifyCost.toFixed(2)}`
                : `Uses your ${classifierProviderLabel}`
            }
          >
            Reclassify ({selectedClassifyIds.length}
            {selectedClassifyIds.length > 0
              ? ` · ${menuWorkload(selectedClassifyChars).formatted}`
              : ""}
            {classifierUsesApi
              ? ` · ~$${selectedClassifyCost.toFixed(2)}`
              : ` · ${classifierProviderLabel}`})
          </button>
          {selectedIds.length > 0 && (
            <button
              onClick={() => setSelectedIds([])}
              className="ml-auto text-xs font-semibold text-slate-500 hover:text-slate-800 hover:underline"
            >
              Clear selection
            </button>
          )}
          <span className="w-full text-[11px] text-slate-400 sm:ml-auto sm:w-auto">
            Paused restaurants cannot be selected; one-off row actions still work.
          </span>
        </div>

        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          {loading ? (
            <div className="p-8 text-center text-slate-400">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="p-8 text-center text-slate-400">
              No restaurants. Click “Run discovery” to populate.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1840px] text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="w-10 px-3 py-3 text-center font-medium">
                      <input
                        type="checkbox"
                        checked={allFilteredSelected}
                        onChange={toggleAllFiltered}
                        disabled={selectableFiltered.length === 0}
                        title="Select all refresh-enabled restaurants in the filtered results"
                        aria-label="Select all filtered restaurants"
                        className="h-4 w-4 rounded border-slate-300 accent-emerald-600"
                      />
                    </th>
                    <th className="px-4 py-3 font-medium">Name</th>
                    <th className="px-4 py-3 text-center font-medium">Refresh</th>
                    <th className="px-4 py-3 font-medium">Rating</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium">Veg?</th>
                    <th className="px-4 py-3 font-medium">Address</th>
                    <th className="px-4 py-3 font-medium">Website</th>
                    <th className="px-4 py-3 font-medium">Menu size / estimate</th>
                    <th className="px-4 py-3 font-medium">Menu score</th>
                    <th className="px-4 py-3 font-medium">Last classified</th>
                    <th className="px-4 py-3 font-medium">Vegan meals / sides</th>
                    {/* Sticky pinning only from sm up: on a phone a pinned
                        430px column covers the whole viewport and makes the
                        table look unscrollable. */}
                    <th className="z-10 border-l border-slate-200 bg-slate-50 px-4 py-3 font-medium sm:sticky sm:right-0 sm:min-w-[430px] sm:shadow-[-8px_0_12px_-12px_rgba(15,23,42,0.45)]">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {groupedFiltered.map((group) => {
                    const groupSelectable = group.items.filter(
                      (restaurant) =>
                        restaurant.refresh_enabled && restaurant.is_consumer_venue
                    );
                    const groupSelected =
                      groupSelectable.length > 0 &&
                      groupSelectable.every((restaurant) =>
                        selectedIds.includes(restaurant.id)
                      );
                    return (
                      <Fragment key={group.label}>
                        {groupBy !== "none" && (
                          <tr className="bg-slate-100/90">
                            <td colSpan={13} className="px-3 py-2">
                              <div className="flex items-center gap-2">
                                <input
                                  type="checkbox"
                                  checked={groupSelected}
                                  onChange={() => toggleGroup(group.items)}
                                  disabled={groupSelectable.length === 0}
                                  aria-label={`Select group ${group.label}`}
                                  className="h-4 w-4 rounded border-slate-300 accent-emerald-600 disabled:cursor-not-allowed"
                                />
                                <span className="text-xs font-bold uppercase tracking-wide text-slate-600">
                                  {group.label}
                                </span>
                                <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-500 shadow-sm">
                                  {group.items.length}
                                </span>
                              </div>
                            </td>
                          </tr>
                        )}
                        {group.items.map((r) => (
                    <tr
                      key={r.place_id}
                      className={`group hover:bg-slate-50 ${
                        r.refresh_enabled ? "" : "bg-slate-50/70 text-slate-400"
                      }`}
                    >
                      <td className="px-3 py-3 text-center">
                        <input
                          type="checkbox"
                          checked={selectedIds.includes(r.id)}
                          onChange={() => toggleSelected(r.id)}
                          disabled={!r.refresh_enabled || !r.is_consumer_venue}
                          title={
                            !r.refresh_enabled
                              ? "Enable refreshes before selecting this restaurant"
                              : !r.is_consumer_venue
                                ? "Non-consumer venues are excluded from batch jobs"
                                : "Select restaurant for a batch action"
                          }
                          aria-label={`Select ${r.name}`}
                          className="h-4 w-4 rounded border-slate-300 accent-emerald-600 disabled:cursor-not-allowed"
                        />
                      </td>
                      <td
                        className="px-4 py-3 font-medium text-slate-900"
                        title={r.editorial_summary || ""}
                      >
                        {r.name}
                        {!r.is_consumer_venue && (
                          <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-800">
                            {r.consumer_hidden ? "hidden from Explore" : "non-restaurant"}
                          </span>
                        )}
                        {r.editorial_summary && (
                          <span className="ml-1 text-slate-300" title={r.editorial_summary}>
                            ⓘ
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-center">
                        <label className="inline-flex cursor-pointer items-center gap-2" title={r.refresh_enabled ? "Included in bulk refresh jobs" : "Paused from bulk refresh jobs"}>
                          <input
                            type="checkbox"
                            checked={Boolean(r.refresh_enabled)}
                            onChange={() => toggleRefreshEnabled(r)}
                            className="h-4 w-4 rounded border-slate-300 accent-emerald-600"
                            aria-label={`Enable refreshes for ${r.name}`}
                          />
                          <span className={`text-[10px] font-bold uppercase tracking-wide ${
                            r.refresh_enabled ? "text-emerald-700" : "text-slate-400"
                          }`}>
                            {r.refresh_enabled ? "on" : "paused"}
                          </span>
                        </label>
                      </td>
                      <td className="px-4 py-3">
                        <RatingBadge
                          rating={r.rating}
                          userRatingCount={r.user_rating_count}
                        />
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-col items-start gap-1">
                          <OpenStatusBadge
                            openNow={r.open_now}
                            enrichedAt={r.enriched_at}
                            openingHours={r.opening_hours}
                          />
                          <FreshnessBadge fetchedAt={r.menu_fetched_at} compact />
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        {r.serves_vegetarian === 1 ? (
                          <span className="text-emerald-600" title="Google: serves vegetarian food">
                            ✓
                          </span>
                        ) : r.serves_vegetarian === 0 ? (
                          <span className="text-slate-400" title="Google: does not serve vegetarian food">
                            ✗
                          </span>
                        ) : (
                          <span className="text-slate-300" title="Unknown">
                            ?
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-slate-600">{r.address}</td>
                      <td className="px-4 py-3">
                        {r.website_url ? (
                          <a
                            href={r.website_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-emerald-600 hover:underline"
                          >
                            visit
                          </a>
                        ) : (
                          <span className="text-slate-300">—</span>
                        )}
                      </td>
                      <td className="min-w-40 px-4 py-3">
                        {r.has_menu_text ? (() => {
                          const workload = menuWorkload(r.menu_chars);
                          return (
                            <div className="space-y-1">
                              <div className="flex items-center gap-1.5">
                                <span className="font-semibold tabular-nums text-slate-700">
                                  {workload.formatted}
                                </span>
                                <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${workload.style}`}>
                                  {workload.label}
                                </span>
                              </div>
                              <div
                                className="text-[11px] text-slate-400"
                                title="Very rough runtime; provider speed, load, and number of dishes can change it substantially"
                              >
                                rough time {workload.runtime}
                              </div>
                              <div className="text-[11px] font-semibold text-violet-600">
                                {classifierUsesApi
                                  ? `${classifierProviderLabel} est ~$${(r.classify_estimate ?? 0).toFixed(2)}`
                                  : classifierProviderLabel}
                              </div>
                            </div>
                          );
                        })() : (
                          <span className="text-xs text-slate-300">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {r.menu_score != null ? (
                          <span
                            className={`inline-flex min-w-12 justify-center rounded-full px-2 py-0.5 text-xs font-bold tabular-nums ${
                              r.menu_score >= 0.75
                                ? "bg-emerald-100 text-emerald-800"
                                : r.menu_score_is_menu
                                  ? "bg-amber-100 text-amber-800"
                                  : "bg-rose-100 text-rose-700"
                            }`}
                            title={r.menu_score_reason || "Menu-likeness score from 0 to 1"}
                          >
                            {r.menu_score.toFixed(2)}
                          </span>
                        ) : (
                          <span className="text-xs text-slate-300">—</span>
                        )}
                      </td>
                      <td className="min-w-40 px-4 py-3">
                        {r.last_classified_at ? (
                          <div
                            className="text-xs text-slate-700"
                            title={new Date(r.last_classified_at).toLocaleString()}
                          >
                            <div className="font-medium">
                              {classificationDate(r.last_classified_at)}
                            </div>
                            <div className="mt-0.5 text-[11px] text-slate-400">
                              {relativeDate(r.last_classified_at)}
                            </div>
                          </div>
                        ) : (
                          <span className="text-xs font-medium text-amber-600">Never</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {r.dish_count > 0 ? (
                          <button
                            onClick={() => setDishesFor(r)}
                            className={`rounded px-2 py-0.5 text-xs font-medium hover:opacity-80 ${
                              r.vegan_options > 0
                                ? "bg-emerald-100 text-emerald-800"
                                : "bg-slate-100 text-slate-500"
                            }`}
                            title="View dishes and verdicts"
                          >
                            {r.vegan_options} meals
                            {(r.vegan_sides || 0) > 0 && ` · ${r.vegan_sides} sides`}
                          </button>
                        ) : (
                          <span className="text-xs text-slate-300">—</span>
                        )}
                      </td>
                      <td className="z-[1] whitespace-nowrap border-l border-slate-100 bg-white px-4 py-3 group-hover:bg-slate-50 sm:sticky sm:right-0 sm:min-w-[430px] sm:shadow-[-8px_0_12px_-12px_rgba(15,23,42,0.35)]">
                        <div className="flex gap-1.5">
                          <button
                            onClick={() => openMenu(r)}
                            disabled={!r.has_menu_text}
                            title={
                              r.has_menu_text
                                ? "View the stored menu text and menu score"
                                : "No menu text has been stored"
                            }
                            className="rounded border border-emerald-200 px-2 py-0.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-300"
                          >
                            view menu
                          </button>
                          <button
                            onClick={() => setHistoryFor(r)}
                            title="Menu versions over time and the dish-change log (added/removed dishes, price moves)"
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50"
                          >
                            🕘 history
                          </button>
                          <button
                            onClick={() => toggleArchived(r)}
                            title={
                              r.archived
                                ? "Restore this listing to the active table (and to pipeline/consumer eligibility rules)"
                                : "Archive: remove from this table, Explore, and all bulk runs — data is kept and it can be restored anytime"
                            }
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50"
                          >
                            {r.archived ? "restore" : "archive"}
                          </button>
                          <button
                            onClick={() => toggleVisibility(r)}
                            disabled={!r.is_consumer_venue && !r.consumer_hidden}
                            title={
                              !r.is_consumer_venue && !r.consumer_hidden
                                ? "Automatically excluded by its Google place type"
                                : r.consumer_hidden
                                  ? "Restore this listing to Explore"
                                  : "Hide this listing from Explore"
                            }
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
                          >
                            {!r.is_consumer_venue && !r.consumer_hidden
                              ? "excluded"
                              : r.consumer_hidden
                                ? "show"
                                : "hide"}
                          </button>
                          <button
                            onClick={() => {
                              setDeleteFor(r);
                              setDeleteConfirm("");
                            }}
                            disabled={rowBusy !== null || ingesting || classifying || deleting}
                            title="Permanently delete this restaurant and all related data"
                            className="rounded border border-rose-200 px-2 py-0.5 text-xs text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            delete
                          </button>
                          <button
                            onClick={() => runRowAction(r, "ingest")}
                            disabled={rowBusy !== null || !r.website_url}
                            title={
                              r.website_url
                                ? "Re-run the menu scraper for this restaurant"
                                : "No website to scrape"
                            }
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {rowBusy?.id === r.id && rowBusy.action === "ingest"
                              ? "scraping…"
                              : "↻ rescrape"}
                          </button>
                          <button
                            onClick={() => runRowAction(r, "classify")}
                            disabled={classifying || rowBusy !== null || !r.has_menu_text}
                            title={
                              r.has_menu_text
                                ? (classifierUsesApi
                                    ? `Re-run classification with ${classifierProviderLabel} (est ~$${(
                                        r.classify_estimate ?? 0.1
                                      ).toFixed(2)} for ${r.menu_chars?.toLocaleString() ?? "?"} chars)`
                                    : `Re-run classification with your ${classifierProviderLabel}`) +
                                  (classifyMode === "full"
                                    ? " — full re-extraction, even if the menu text is unchanged"
                                    : " — changes only; skipped when the menu text is unchanged")
                                : "No menu text to classify"
                            }
                            className="rounded border border-emerald-200 px-2 py-0.5 text-xs text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {rowBusy?.id === r.id && rowBusy.action === "classify"
                              ? "classifying…"
                              : "⚡ reclassify"}
                          </button>
                          {r.has_menu_text && (
                            <span
                              className="self-center whitespace-nowrap text-[10px] text-slate-400"
                              title={
                                r.last_classify_cost != null
                                  ? "Actual cost of the last classification run"
                                  : PROVIDER_LABELS[r.last_classify_provider] &&
                                      r.last_classify_provider !== "anthropic"
                                    ? `Last classified with ${PROVIDER_LABELS[r.last_classify_provider]}`
                                    : classifierUsesApi
                                      ? "Estimate from menu size"
                                      : `Uses your ${classifierProviderLabel}`
                              }
                            >
                              {r.last_classify_cost != null
                                ? `$${r.last_classify_cost.toFixed(2)}`
                                : PROVIDER_LABELS[r.last_classify_provider] &&
                                    r.last_classify_provider !== "anthropic"
                                  ? r.last_classify_provider
                                  : classifierUsesApi
                                    ? `~$${(r.classify_estimate ?? 0).toFixed(2)} est`
                                    : "subscription"}
                            </span>
                          )}
                        </div>
                      </td>
                    </tr>
                        ))}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
        </>
        )}
      </div>

      {deleteFor && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/50 p-4"
          onClick={() => {
            if (!deleting) {
              setDeleteFor(null);
              setDeleteConfirm("");
            }
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              permanentlyDeleteRestaurant();
            }}
            onClick={(event) => event.stopPropagation()}
            className="w-full max-w-md rounded-xl bg-white p-5 shadow-2xl"
          >
            <h2 className="text-lg font-bold text-rose-800">Permanently delete restaurant?</h2>
            <p className="mt-2 text-sm leading-relaxed text-slate-600">
              This permanently removes <strong>{deleteFor.name}</strong>, its menu sources,
              dishes, classifications, reports, crawl history, and menu history. This cannot
              be undone. Use Archive instead if you may want the data later.
            </p>
            <label className="mt-4 block text-xs font-bold text-slate-700">
              Type <span className="select-all text-rose-700">{deleteFor.name}</span> to confirm
              <input
                autoFocus
                value={deleteConfirm}
                onChange={(event) => setDeleteConfirm(event.target.value)}
                className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-rose-500 focus:ring-1 focus:ring-rose-500"
                autoComplete="off"
              />
            </label>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setDeleteFor(null);
                  setDeleteConfirm("");
                }}
                disabled={deleting}
                className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-bold text-slate-700 hover:bg-slate-50 disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={deleting || deleteConfirm !== deleteFor.name}
                className="rounded-lg bg-rose-700 px-4 py-2 text-sm font-bold text-white hover:bg-rose-800 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {deleting ? "Deleting…" : "Delete permanently"}
              </button>
            </div>
          </form>
        </div>
      )}

      {addOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
          onClick={() => !adding && setAddOpen(false)}
        >
          <div
            className="flex max-h-[85vh] w-full max-w-lg flex-col rounded-xl bg-white shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <h2 className="font-semibold text-slate-900">Add restaurants by name</h2>
              <button
                onClick={() => !adding && setAddOpen(false)}
                className="text-slate-400 hover:text-slate-700"
              >
                ✕
              </button>
            </div>
            <div className="space-y-3 overflow-y-auto p-4">
              {addResolved === null ? (
                <>
                  <p className="text-sm text-slate-500">
                    One name per line. Matches are shown for confirmation
                    before anything is added — you pick the exact place and
                    which pipeline steps to run.
                  </p>
                  <textarea
                    value={addNames}
                    onChange={(e) => setAddNames(e.target.value)}
                    rows={5}
                    placeholder={"Ethos Vegan Kitchen\n4Rivers Smokehouse Winter Park"}
                    className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
                  />
                  <button
                    onClick={resolveAddNames}
                    disabled={resolving || !addNames.trim()}
                    className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                  >
                    {resolving ? "Searching Google Places…" : "Find matches"}
                  </button>
                </>
              ) : (
                <>
                  <div className="flex items-center justify-between">
                    <p className="text-sm font-semibold text-slate-700">
                      Confirm the right place for each name
                    </p>
                    <button
                      onClick={() => setAddResolved(null)}
                      disabled={adding}
                      className="text-xs font-semibold text-slate-400 hover:text-slate-700 hover:underline"
                    >
                      ← Edit names
                    </button>
                  </div>
                  {addResolved.map((entry) => (
                    <fieldset
                      key={entry.query}
                      className="rounded-lg border border-slate-200 p-3"
                    >
                      <legend className="px-1 text-xs font-bold uppercase tracking-wide text-slate-400">
                        {entry.query}
                      </legend>
                      {entry.candidates.length === 0 && (
                        <p className="text-sm text-amber-700">
                          No Google Places match found.
                        </p>
                      )}
                      <div className="space-y-1.5">
                        {entry.candidates.map((c) => (
                          <label
                            key={c.place_id}
                            className="flex cursor-pointer items-start gap-2 text-sm"
                          >
                            <input
                              type="radio"
                              name={`cand-${entry.query}`}
                              checked={addSelections[entry.query] === c.place_id}
                              onChange={() =>
                                setAddSelections((s) => ({
                                  ...s,
                                  [entry.query]: c.place_id,
                                }))
                              }
                              className="mt-1"
                            />
                            <span>
                              <span className="font-medium text-slate-900">
                                {c.name}
                              </span>
                              {!c.name_overlap && (
                                <span className="ml-1.5 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold uppercase text-amber-800">
                                  weak match
                                </span>
                              )}
                              {c.already_added_id != null && (
                                <span className="ml-1.5 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-bold uppercase text-sky-800">
                                  already added — re-adding refreshes it
                                </span>
                              )}
                              <span className="block text-xs text-slate-500">
                                {c.address}
                              </span>
                            </span>
                          </label>
                        ))}
                        {entry.candidates.length > 0 && (
                          <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-500">
                            <input
                              type="radio"
                              name={`cand-${entry.query}`}
                              checked={!addSelections[entry.query]}
                              onChange={() =>
                                setAddSelections((s) => ({
                                  ...s,
                                  [entry.query]: "",
                                }))
                              }
                            />
                            Don't add this one
                          </label>
                        )}
                      </div>
                    </fieldset>
                  ))}
                  <div className="rounded-lg bg-slate-50 p-3">
                    <p className="mb-2 text-xs font-bold uppercase tracking-wide text-slate-400">
                      Run immediately after adding
                    </p>
                    <label className="flex items-center gap-2 text-sm text-slate-700">
                      <input
                        type="checkbox"
                        checked={addIngest}
                        onChange={(e) => setAddIngest(e.target.checked)}
                      />
                      Scrape menu now (~30s–1 min each)
                    </label>
                    <label
                      className={`mt-1 flex items-center gap-2 text-sm ${
                        addIngest ? "text-slate-700" : "text-slate-400"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={addIngest && addClassify}
                        disabled={!addIngest}
                        onChange={(e) => setAddClassify(e.target.checked)}
                      />
                      Classify dishes now (needs the menu; uses the selected
                      provider)
                    </label>
                    <p className="mt-1.5 text-xs text-slate-400">
                      Enrichment (Google ratings, hours, food signals) always
                      runs. Anything skipped here can be run later from the
                      table.
                    </p>
                  </div>
                  <button
                    onClick={confirmAdd}
                    disabled={
                      adding ||
                      !Object.values(addSelections).some(Boolean)
                    }
                    className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                  >
                    {adding
                      ? "Adding (this can take a minute per restaurant)…"
                      : `Add ${
                          Object.values(addSelections).filter(Boolean).length
                        } restaurant${
                          Object.values(addSelections).filter(Boolean).length === 1
                            ? ""
                            : "s"
                        }`}
                  </button>
                </>
              )}
              {addResult?.error && (
                <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
                  {addResult.error}
                </div>
              )}
              {addResult?.added?.length > 0 && (
                <ul className="space-y-1 text-sm">
                  {addResult.added.map((entry) => (
                    <li key={entry.id} className="rounded bg-emerald-50 px-3 py-1.5">
                      <span className="font-medium text-emerald-800">{entry.name}</span>
                      <span className="ml-1 text-emerald-700/70">
                        — added
                        {entry.scraped != null &&
                          (entry.scraped ? ", menu scraped" : ", menu scrape failed")}
                        {entry.dishes != null && `, ${entry.dishes} dishes classified`}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}

      {dishesFor && (
        <DishModal restaurant={dishesFor} onClose={() => setDishesFor(null)} />
      )}

      {historyFor && (
        <HistoryModal restaurant={historyFor} onClose={() => setHistoryFor(null)} />
      )}

      {menuFor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
          onClick={() => setMenuFor(null)}
        >
          <div
            className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-xl bg-white shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <div>
                <h2 className="font-semibold text-slate-900">
                  Scraped menu — {menuFor.name}
                </h2>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
                  {menuScore != null && (
                    <span className="rounded bg-emerald-100 px-2 py-0.5 font-medium text-emerald-700">
                      menu score {menuScore.toFixed(2)}
                    </span>
                  )}
                  {menuFor.has_menu_text && (() => {
                    const workload = menuWorkload(menuFor.menu_chars);
                    return (
                      <>
                        <span className="font-semibold text-slate-600">{workload.formatted}</span>
                        <span className={`rounded-full px-2 py-0.5 font-bold ${workload.style}`}>
                          {workload.label}
                        </span>
                        <span className="text-slate-400">roughly {workload.runtime}</span>
                        <span className="font-semibold text-violet-600">
                          {classifierUsesApi
                            ? `${classifierProviderLabel} est ~$${(menuFor.classify_estimate ?? 0).toFixed(2)}`
                            : classifierProviderLabel}
                        </span>
                      </>
                    );
                  })()}
                </div>
              </div>
              <button
                onClick={() => setMenuFor(null)}
                className="text-slate-400 hover:text-slate-700"
              >
                ✕
              </button>
            </div>
            <div className="overflow-y-auto p-4">
              {menuLoading ? (
                <div className="text-slate-400">Loading…</div>
              ) : (
                <pre className="whitespace-pre-wrap break-words font-mono text-xs text-slate-700">
                  {menuText}
                </pre>
              )}
            </div>
            <div className="border-t border-slate-200 px-4 py-2 text-xs text-slate-400">
              Raw scraped text — exactly what the classifier reads when
              extracting this restaurant's dishes.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
