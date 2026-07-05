import { Fragment, useEffect, useMemo, useState } from "react";
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

// Display names for the classification providers. "auto" walks claude then
// codex (subscriptions only, failing over on usage limits); the metered
// Anthropic API runs only when explicitly selected.
const PROVIDER_LABELS = {
  claude: "Claude subscription",
  codex: "Codex subscription",
  anthropic: "Anthropic API",
};

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

export default function Admin() {
  const [restaurants, setRestaurants] = useState([]);
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [enriching, setEnriching] = useState(false);
  const [notice, setNotice] = useState(null);
  const [query, setQuery] = useState("");
  const [operationalFilter, setOperationalFilter] = useState("all");
  const [groupBy, setGroupBy] = useState("none");
  const [menuFor, setMenuFor] = useState(null); // restaurant whose menu is open
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
  const [providerUsage, setProviderUsage] = useState(null); // subscription limits
  const [selectedIds, setSelectedIds] = useState([]);
  const [classifierProvider, setClassifierProvider] = useState("auto");

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      const [rRes, cRes, reportRes, qualityRes, usageRes] = await Promise.all([
        fetch("/api/restaurants?include_excluded=true"),
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
        body: JSON.stringify({ provider: classifierProvider }),
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
          ...(action === "classify" ? { provider: classifierProvider } : {}),
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
  const classifierUsesApi = resolvedClassifierProvider === "anthropic";
  const classifierProviderLabel =
    PROVIDER_LABELS[resolvedClassifierProvider] || "No provider available";

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const qualityIds = new Set(menuQuality.map((finding) => finding.restaurant_id));
    return restaurants.filter((restaurant) => {
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
  }, [restaurants, query, operationalFilter, menuQuality]);

  const groupedFiltered = useMemo(() => {
    if (groupBy === "none") return [{ label: "All restaurants", items: filtered }];

    const labelFor = (restaurant) => {
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
        groupBy === "classification_age"
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
    return result;
  }, [filtered, groupBy]);

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
      <div className="w-full max-w-none px-4 py-8 sm:px-6 lg:px-8">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">VeganFind — Pipeline Dashboard</h1>
            <p className="text-sm text-slate-500">
              Discovery · menu scraping · dish classification
              {config?.city ? ` · ${config.city}, FL` : ""}
            </p>
          </div>
          <div className="flex flex-wrap justify-end gap-2">
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
              <option value="auto">
                Auto — subscriptions only (
                {PROVIDER_LABELS[config?.classifier?.resolved] ||
                  "none available"}
                )
              </option>
              <option
                value="claude"
                disabled={!config?.classifier?.providers?.claude?.available}
              >
                Claude subscription
              </option>
              <option
                value="codex"
                disabled={!config?.classifier?.providers?.codex?.available}
              >
                Codex subscription
              </option>
              <option
                value="anthropic"
                disabled={!config?.classifier?.providers?.anthropic?.available}
              >
                Anthropic API
              </option>
            </select>
            <button
              onClick={runClassify}
              disabled={classifying || unclassified === 0}
              className="rounded-lg border border-violet-400 px-4 py-2 text-sm font-semibold text-violet-700 shadow-sm transition hover:bg-violet-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
              title={
                classifierUsesApi
                  ? `Classify new menus with Anthropic — est ~$${classifyCostNew.toFixed(2)} API cost`
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

        <JobProgressPanel job={ingestJob} title="Scraping menus" />
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
              {["claude", "codex"].map((name) => {
                const usage = providerUsage[name];
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
                      <div className="mt-1 space-y-1">
                        {usage.windows.map((w) => (
                          <UsageBar key={w.id} window={w} />
                        ))}
                      </div>
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
                {menuQuality.length} {qualityOpen ? "▾" : "▸"}
              </span>
            </button>
            {qualityOpen && (
              <div className="mt-3 space-y-2">
                {menuQuality.map((f) => (
                  <div
                    key={f.restaurant_id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-white p-3 text-sm shadow-sm"
                  >
                    <div>
                      <div className="font-bold text-slate-900">{f.name}</div>
                      <ul className="mt-0.5 text-xs text-orange-800">
                        {f.flags.map((flag) => (
                          <li key={flag}>• {flag}</li>
                        ))}
                      </ul>
                    </div>
                    <button
                      onClick={() => {
                        const r = restaurants.find((x) => x.id === f.restaurant_id);
                        if (r) runRowAction(r, "ingest");
                      }}
                      disabled={rowBusy !== null}
                      className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {rowBusy?.id === f.restaurant_id && rowBusy.action === "ingest"
                        ? "scraping…"
                        : "↻ rescrape"}
                    </button>
                  </div>
                ))}
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

        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name or address…"
            className="w-full max-w-sm rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
          />
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
            <option value="refresh">Group by refresh status</option>
            <option value="pipeline">Group by pipeline stage</option>
            <option value="freshness">Group by menu freshness</option>
            <option value="classification_age">Group by last classified</option>
          </select>
          {(query || operationalFilter !== "all" || groupBy !== "none") && (
            <button
              onClick={() => {
                setQuery("");
                setOperationalFilter("all");
                setGroupBy("none");
              }}
              className="text-xs font-semibold text-slate-500 hover:text-slate-800 hover:underline"
            >
              Reset view
            </button>
          )}
          <span className="ml-auto whitespace-nowrap text-sm text-slate-500">
            {filtered.length} shown
          </span>
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 shadow-sm">
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
                    <th className="sticky right-0 z-10 min-w-[430px] border-l border-slate-200 bg-slate-50 px-4 py-3 font-medium shadow-[-8px_0_12px_-12px_rgba(15,23,42,0.45)]">
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
                          <OpenStatusBadge openNow={r.open_now} enrichedAt={r.enriched_at} />
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
                                  ? `Anthropic est ~$${(r.classify_estimate ?? 0).toFixed(2)}`
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
                      <td className="sticky right-0 z-[1] min-w-[430px] whitespace-nowrap border-l border-slate-100 bg-white px-4 py-3 shadow-[-8px_0_12px_-12px_rgba(15,23,42,0.35)] group-hover:bg-slate-50">
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
                                ? classifierUsesApi
                                  ? `Re-run classification with Anthropic (est ~$${(
                                      r.classify_estimate ?? 0.1
                                    ).toFixed(2)} for ${r.menu_chars?.toLocaleString() ?? "?"} chars)`
                                  : `Re-run classification with your ${classifierProviderLabel}`
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
      </div>

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
                            ? `Anthropic est ~$${(menuFor.classify_estimate ?? 0).toFixed(2)}`
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
